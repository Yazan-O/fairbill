"""Fairbill audit engine.

A Strands Graph runs three specialists in parallel over one bill, then a
deterministic validator node re-checks every claim against the hospital's own
price-file rows. A claim that cannot be re-derived from the file is dropped and
the reason is written into AuditResult.notes, so nothing reaches the patient
that the file does not support.

    python -m fairbill.audit --bench
    python -m fairbill.audit --bill bill_01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from strands import Agent
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.multiagent.graph import GraphBuilder, GraphState

from fairbill.codes import PANELS, describe
from fairbill.config import down_reason, live_ladder, make_model, mark_down, model_name, retry_strategy
from fairbill.schema import AuditResult, Bill, BillLine, FileRow, Finding, FindingKind
from fairbill.tools import code_lookup, file_rows, hospital_resolve, mrf_lookup, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = REPO_ROOT / "_runs" / "2026-09-13_phase4_audit"
NSA_CITE = (
    "No Surprises Act, 45 CFR 149.420 (non-emergency services by nonparticipating "
    "providers at participating facilities; anesthesiology cannot be balance-billed)"
)

SPECIALISTS = ("line_matcher", "coding_specialist", "rights_specialist")

# Only the top two ED levels are worth questioning from a printed visit summary.
UPCODABLE = ("99284", "99285")
OON_NOTE = re.compile(r"out.of.network|non.participating|not contracted", re.I)


# ---- what a specialist returns -------------------------------------------

class Claim(BaseModel):
    """One suspected problem. Evidence is named by code, not copied: the validator
    pulls the real price-file row itself."""
    kind: FindingKind
    line_nos: list[int] = Field(description="Bill line numbers involved, as printed")
    summary: str = Field(description="One sentence a patient can read")
    evidence_codes: list[str] = Field(default_factory=list, description="Price-file codes that prove it")
    note_quotes: list[str] = Field(
        default_factory=list,
        description="For an upcoded claim only: 1 to 3 short phrases copied character for character "
                    "out of the printed visit summary that show the visit was a lower level. "
                    "A claim with no quote, or with a phrase that is not in the summary, is dropped.")
    needs_user_judgment: bool = False
    confidence: float = 1.0


class Claims(BaseModel):
    findings: list[Claim] = Field(default_factory=list)


# ---- deterministic context ------------------------------------------------

def _money(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _bill_table(bill: Bill) -> str:
    out = [
        f"Hospital: {bill.hospital_name}",
        f"Payer: {bill.payer}   Visit type: {bill.visit_type or 'unknown'}",
        f"Service dates: {bill.service_date_start or '?'} to {bill.service_date_end or '?'}",
    ]
    if bill.clinical_note:
        out.append(f"Visit summary printed on the bill: {bill.clinical_note}")
    out.append("")
    out.append("line | code | description | units | charge | patient price | provider | network note")
    for ln in bill.lines:
        out.append(
            f"{ln.line_no} | {ln.code or '-'} | {ln.description} | {ln.units:g} | "
            f"{_money(ln.charge)} | {_money(ln.patient_price)} | {ln.provider or '-'} | {ln.network_note or '-'}"
        )
    return "\n".join(out)


def _row_for_line(hospital_id: str, code: str, charge: Optional[float]) -> Optional[dict]:
    """The file row that matches this bill line: same gross charge when one exists,
    otherwise the best-ranked row for the code."""
    if charge is not None:
        # the bill prints the gross charge, so the row carrying that exact gross is the
        # one the hospital billed from, wherever it sits in the file; ties go to the
        # earliest line so the citation is stable
        hits = [r for r in file_rows(hospital_id, code, limit=500)
                if r["gross"] is not None and abs(r["gross"] - charge) < 0.005]
        if hits:
            return min(hits, key=lambda r: r["source_line"])
    rows = file_rows(hospital_id, code)
    return rows[0] if rows else None


def _file_table(bill: Bill, hospital_id: str) -> str:
    out = ["The hospital's own published standard-charges file says:",
           "code | posted gross | posted cash price | file line | description"]
    seen = set()
    for ln in bill.lines:
        if not ln.code or ln.code in seen:
            continue
        seen.add(ln.code)
        r = _row_for_line(hospital_id, ln.code, ln.charge)
        if r is None:
            out.append(f"{ln.code} | not in the file | | | (no row published for this code)")
        else:
            out.append(f"{r['code']} | {_money(r['gross'])} | {_money(r['discounted_cash'])} | "
                       f"{r['source_line']} | {r['description']}")
    # panels the file also prices, when the bill carries their components
    on_bill = {ln.code for ln in bill.lines if ln.code}
    for panel, comps in PANELS.items():
        if panel in on_bill or len(on_bill & set(comps)) < 3:
            continue
        rows = file_rows(hospital_id, panel)
        if rows:
            r = rows[0]
            out.append(f"{panel} | {_money(r['gross'])} | {_money(r['discounted_cash'])} | "
                       f"{r['source_line']} | {r['description']} (single panel row covering "
                       f"{', '.join(sorted(on_bill & set(comps)))})")
    return "\n".join(out)


def _code_table(bill: Bill) -> str:
    out = ["What the codes mean:"]
    seen = set()
    for ln in bill.lines:
        if not ln.code or ln.code in seen:
            continue
        seen.add(ln.code)
        d = describe(ln.code)
        if not d["found"]:
            out.append(f"{ln.code}: not in the reference table")
            continue
        extra = ""
        if d.get("component_of"):
            extra += f" [component of panel {', '.join(d['component_of'])}]"
        if d.get("level_ladder"):
            extra += f" [level {d['level']} of {len(d['level_ladder'])}: {' < '.join(d['level_ladder'])}]"
        out.append(f"{ln.code}: {d['meaning']}{extra}")
    return "\n".join(out)


def build_context(bill: Bill, hospital_id: str) -> str:
    return "\n\n".join([_bill_table(bill), _file_table(bill, hospital_id), _code_table(bill)])


# ---- graph nodes ----------------------------------------------------------

def _text_result(node_id: str, text: str) -> MultiAgentResult:
    from strands.agent.agent_result import AgentResult
    from strands.telemetry.metrics import EventLoopMetrics
    ar = AgentResult(stop_reason="end_turn",
                     message={"role": "assistant", "content": [{"text": text}]},
                     metrics=EventLoopMetrics(), state={})
    return MultiAgentResult(status=Status.COMPLETED,
                            results={node_id: NodeResult(result=ar, status=Status.COMPLETED)})


class ContextNode(MultiAgentBase):
    """Entry node: hands the specialists the bill, the matching price-file rows and
    the code meanings, all already resolved."""

    def __init__(self, node_id: str = "context"):
        super().__init__()
        self.node_id = node_id

    async def invoke_async(self, task, invocation_state=None, **kwargs) -> MultiAgentResult:
        st = invocation_state or {}
        return _text_result(self.node_id, st.get("context", ""))


SYSTEM_PROMPTS = {
    "line_matcher": """You audit one hospital bill against the hospital's own posted prices.

Report only these two problems:
- duplicate: two lines carry the same code, the same charge and the same date of
  service, and the visit would not plausibly need the test twice that day.
- above_cash: on a self-pay bill, the patient price charged on a line is higher than
  the posted cash price for that code in the file above.

Rules. Compare the patient price column to the posted cash price; ignore the gross
charge. Equal prices are not a problem. A code with no row in the file is not your
problem. Two lines with the same code but different charges, different units or
different dates are not a duplicate. A bill that names an insurance plan has no cash
price to compare against, so report no above_cash on it. If nothing is wrong, return an
empty findings list. Never invent a line number or a code. Do not report coding,
bundling, visit level or insurance problems.""",
    "coding_specialist": """You audit the coding on one hospital bill.

Report only these two problems:
- unbundled: the bill charges separately for three or more tests that are components of
  one panel, the panel itself is not billed on the same date, and the hospital's own
  file publishes a price for that panel. List every component line in line_nos and put
  the panel code first in evidence_codes.
- upcoded: a visit level code is higher than the printed visit summary supports. Judge
  from the visit summary text only. Put the level code the summary does support in
  evidence_codes, first. Fill note_quotes with 1 to 3 short phrases copied out of the
  printed visit summary, character for character, that show the visit was milder than
  the level billed; a claim whose quotes are not found in the summary is thrown away.
  Set needs_user_judgment true on every upcoded finding, because only the medical
  record settles the level.

Rules. Never report unbundling unless three or more components of the same panel are
billed separately and the file above actually prices the panel row. A panel billed
once, alone, is correct. Do not report a visit level when no visit summary is printed.
Do not report duplicates, prices or insurance problems. If nothing is wrong, return an
empty findings list.""",
    "rights_specialist": """You check one hospital bill for patient protections against surprise bills.

Report only:
- out_of_network: a line names a provider or carries a note saying the provider is out
  of network or not contracted with the plan, while the facility itself is in network.

Rules. Only flag a line that actually carries out-of-network wording on the bill. A
self-pay bill with no insurance plan has no network, so report nothing. Do not report
prices, duplicates or coding problems. If nothing is wrong, return an empty findings
list.""",
}

TOOLS = {
    "line_matcher": [mrf_lookup],
    "coding_specialist": [code_lookup, mrf_lookup],
    "rights_specialist": [hospital_resolve],
}


class SpecialistNode(MultiAgentBase):
    """One specialist agent. Never raises: the Python Graph is fail-fast, so a model
    error is caught, retried on the same-bar fallback model, and otherwise reported
    as an empty claim list plus a note."""

    def __init__(self, node_id: str, collector: dict):
        super().__init__()
        self.node_id = node_id
        self.collector = collector

    def _agent(self, model_id: str) -> Agent:
        # Retry math against the 120 s node timeout. Strands' default retry (6 attempts,
        # 4..64 s backoff, about 124 s of waiting alone) overran the node timeout, so the
        # fallback never ran. Three attempts at 2 s then 4 s of backoff is 6 s of waiting plus
        # at most 3 x about 10 s of inference, roughly 36 s for the primary ladder; the Haiku
        # ladder is the same again, so both fit inside 120 s. A single ThrottlingException
        # therefore costs one retry on the primary and never the fallback: only three
        # consecutive primary failures hand this node to Haiku.
        return Agent(
            model=make_model(model=model_id),
            system_prompt=SYSTEM_PROMPTS[self.node_id],
            tools=TOOLS[self.node_id],
            structured_output_model=Claims,
            callback_handler=None,
            retry_strategy=retry_strategy(),
        )

    async def invoke_async(self, task, invocation_state=None, **kwargs) -> MultiAgentResult:
        st = invocation_state or {}
        prompt = st.get("context", "") + "\n\nReport every problem of your kinds, and nothing else."
        claims: list[Claim] = []
        t_start = time.time()
        self.collector.setdefault("timeline", []).append(
            {"node": self.node_id, "event": "start", "t": round(t_start, 3)})
        rungs = live_ladder()
        for model_id in rungs:
            try:
                res = await self._agent(model_id).invoke_async(prompt)
                out = res.structured_output
                claims = list(out.findings) if out else []
                if model_id != rungs[0]:
                    why = down_reason(rungs[0]) or "failed"
                    self.collector["notes"].append(
                        f"{self.node_id}: {model_name(rungs[0])} {why}; used {model_name(model_id)}")
                # membership of "ran" is what marks this specialist's part of the bill as
                # actually checked; a timeout or a cancelled task never reaches this line
                self.collector["ran"].add(self.node_id)
                break
            except Exception as exc:  # noqa: BLE001 - a node must not take the graph down
                mark_down(model_id, exc)
                if model_id == rungs[-1]:
                    self.collector["notes"].append(f"{self.node_id}: every model failed ({type(exc).__name__})")
        self.collector["claims"].extend((self.node_id, c) for c in claims)
        self.collector.setdefault("timeline", []).append(
            {"node": self.node_id, "event": "end", "t": round(time.time(), 3),
             "seconds": round(time.time() - t_start, 2)})
        q = self.collector.get("progress")
        if q is not None:  # one progress event per specialist, for the page's live rows
            q.put_nowait({"specialist": self.node_id, "status": "done",
                          "seconds": round(time.time() - t_start, 2)})
        return _text_result(self.node_id, f"{self.node_id}: {len(claims)} claim(s)")


class ValidatorNode(MultiAgentBase):
    """Deterministic re-check. Every surviving finding carries a real file row."""

    def __init__(self, collector: dict, node_id: str = "validator"):
        super().__init__()
        self.node_id = node_id
        self.collector = collector

    async def invoke_async(self, task, invocation_state=None, **kwargs) -> MultiAgentResult:
        st = invocation_state or {}
        t_start = time.time()
        kept, notes = validate(self.collector["claims"], st["bill"], st["hospital_id"])
        self.collector.setdefault("timeline", []).append(
            {"node": self.node_id, "event": "end", "t": round(time.time(), 3),
             "seconds": round(time.time() - t_start, 2)})
        self.collector["findings"] = kept
        self.collector["validated"] = True
        self.collector["notes"].extend(notes)
        return _text_result(self.node_id, f"validator: kept {len(kept)} finding(s)")


def all_dependencies_complete(required_nodes: list[str]):
    def check_all_complete(state: GraphState) -> bool:
        return all(
            node_id in state.results and state.results[node_id].status == Status.COMPLETED
            for node_id in required_nodes
        )
    return check_all_complete


# ---- validator ------------------------------------------------------------

def _to_row(d: dict) -> FileRow:
    return FileRow(**d)


def _plain(text: str) -> str:
    """Patient-facing copy stays plain ASCII: no dashes standing in for punctuation."""
    out = (text or "").replace(" — ", ", ").replace("—", ", ")
    return out.replace("–", "-").replace("’", "'").strip()


def _line_map(bill: Bill) -> dict[int, BillLine]:
    return {ln.line_no: ln for ln in bill.lines}


def _price(ln: BillLine) -> Optional[float]:
    """What this line actually asks the patient for."""
    return ln.patient_price if ln.patient_price is not None else ln.charge


def _basis_price(ln: BillLine, cash_basis: bool) -> Optional[float]:
    """The line's price on the basis the bill is audited at: a self-pay bill is compared
    cash price to cash price, an insured bill gross charge to gross charge."""
    return ln.patient_price if cash_basis else ln.charge


def _is_self_pay(bill: Bill) -> bool:
    return not bill.payer or "self" in bill.payer.strip().lower()


def _same_date(a: BillLine, b: BillLine) -> bool:
    return (a.date_of_service or None) == (b.date_of_service or None)


def _flat(text: Optional[str]) -> str:
    """Whitespace-collapsed lower case, so a quote matches text a reader wrapped differently."""
    return " ".join((text or "").split()).lower()


def validate(claims: list[tuple[str, Claim]], bill: Bill, hospital_id: str) -> tuple[list[Finding], list[str]]:
    """Re-derive every claim from the bill and the hospital's file. Claims that cannot
    be re-derived are dropped with a reason."""
    lines = _line_map(bill)
    kept: list[Finding] = []
    notes: list[str] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()

    for source, c in claims:
        nos = sorted(set(c.line_nos))
        key = (c.kind, tuple(nos))
        if key in seen:
            continue
        if any(n not in lines for n in nos) or not nos:
            notes.append(f"dropped {c.kind} from {source}: line numbers {c.line_nos} are not on the bill")
            continue
        c.summary = _plain(c.summary)
        f = _check(c, nos, lines, bill, hospital_id)
        if isinstance(f, str):
            note = f"dropped {c.kind} on lines {nos} from {source}: {f}"
            if note not in notes:
                notes.append(note)
            continue
        if f.amount_at_stake is None or f.amount_at_stake <= 0:
            notes.append(f"dropped {c.kind} on lines {nos} from {source}: "
                         f"it does not put any money at stake")
            continue
        seen.add(key)
        kept.append(f)
    kept.sort(key=lambda f: (f.line_nos[0], f.kind))
    return kept, notes


def _check(c: Claim, nos: list[int], lines: dict[int, BillLine], bill: Bill,
           hospital_id: str) -> Finding | str:
    sel = [lines[n] for n in nos]
    cash_basis = _is_self_pay(bill)
    basis: Literal["gross", "cash", "unknown"] = "cash" if cash_basis else "gross"

    if c.kind == "duplicate":
        if len(sel) != 2:
            return "a duplicate must name exactly two lines"
        a, b = sel
        if a.line_no == b.line_no:
            return "a duplicate must name two different lines"
        if not a.code or a.code != b.code or abs(a.charge - b.charge) > 0.005:
            return "the two lines do not carry the same code and the same charge"
        if not _same_date(a, b):
            return "the two lines are for different dates of service"
        row = _row_for_line(hospital_id, a.code, a.charge)
        if row is None:
            return f"code {a.code} has no row in the hospital file"
        return Finding(kind="duplicate", line_nos=nos, evidence=[_to_row(row)], basis=basis,
                       amount_at_stake=round(_price(b) or 0.0, 2), confidence=c.confidence,
                       summary=c.summary or f"{a.description} (code {a.code}) is billed twice on lines {nos[0]} and {nos[1]}.")

    if c.kind == "above_cash":
        if len(sel) != 1:
            return "an above-cash finding must name one line"
        ln = sel[0]
        if not cash_basis:
            return "the bill is billed to a plan, so the posted cash price does not apply"
        if not ln.code:
            return "the line has no code to look up"
        if ln.patient_price is None:
            return "the line prints no patient price to compare with the posted cash price"
        row = _row_for_line(hospital_id, ln.code, ln.charge)
        if row is None or row["discounted_cash"] is None:
            return f"code {ln.code} has no posted cash price in the hospital file"
        paid = ln.patient_price
        if paid < row["discounted_cash"] + 0.01:
            return "the price charged is not above the posted cash price"
        return Finding(kind="above_cash", line_nos=nos, evidence=[_to_row(row)], basis="cash",
                       amount_at_stake=round(paid - row["discounted_cash"], 2), confidence=c.confidence,
                       summary=c.summary or (f"Line {ln.line_no} charges {_money(paid)} for {ln.description}, "
                                             f"above the hospital's posted cash price of {_money(row['discounted_cash'])}."))

    if c.kind == "unbundled":
        codes = {ln.code for ln in sel if ln.code}
        if len(codes) < 3:
            return "unbundling needs at least three component lines"
        panel = next((p for p in [*c.evidence_codes, *PANELS] if p in PANELS and codes <= set(PANELS[p])), None)
        if panel is None:
            return "the lines are not all components of one panel"
        if any(o.code == panel and any(_same_date(o, ln) for ln in sel) for o in lines.values()):
            return f"the bill already charges the {panel} panel itself on the same date"
        prow = next(iter(file_rows(hospital_id, panel)), None)
        if prow is None:
            return f"the hospital file does not price panel {panel}"
        ev = [_to_row(prow)]
        for ln in sel:
            r = _row_for_line(hospital_id, ln.code, ln.charge)
            if r is None:
                return f"component code {ln.code} has no row in the hospital file"
            ev.append(_to_row(r))
        if any(_basis_price(ln, cash_basis) is None for ln in sel):
            return "a component line prints no price on the basis this bill uses"
        parts = sum(_basis_price(ln, cash_basis) or 0.0 for ln in sel)
        bench = prow["discounted_cash"] if cash_basis else prow["gross"]
        if bench is None:
            return f"the hospital file does not price panel {panel} on this bill's basis"
        return Finding(kind="unbundled", line_nos=nos, evidence=ev, basis=basis,
                       amount_at_stake=round(parts - bench, 2), confidence=c.confidence,
                       summary=c.summary or (f"Lines {nos} are the components of one {panel} panel, which this "
                                             f"hospital prices as a single row."))

    if c.kind == "upcoded":
        if len(sel) != 1:
            return "an upcoding finding must name one line"
        ln = sel[0]
        if ln.code not in UPCODABLE:
            return f"code {ln.code} is not one of the visit levels Fairbill questions"
        d = describe(ln.code or "")
        ladder = d.get("level_ladder")
        if not ladder:
            return f"code {ln.code} is not a visit level that can be upcoded"
        note = _flat(bill.clinical_note)
        if not note:
            return "no visit summary is printed on the bill, so the level cannot be questioned"
        quotes = [q for q in c.note_quotes if _flat(q)][:3]
        if not quotes:
            return "the claim quotes nothing from the printed visit summary"
        missing = [q for q in quotes if _flat(q) not in note]
        if missing:
            return f"quoted text is not in the printed visit summary: {missing[0]!r}"
        billed = _row_for_line(hospital_id, ln.code, ln.charge)
        lower_codes = [x for x in c.evidence_codes if x in ladder and ladder.index(x) < ladder.index(ln.code)]
        # no level named: compare against the middle of the ladder, an ordinary visit
        lower_codes.append(ladder[(len(ladder) - 1) // 2])
        lower_codes += [x for x in reversed(ladder[: ladder.index(ln.code)])]
        lower = next((r for x in lower_codes for r in [next(iter(file_rows(hospital_id, x)), None)] if r), None)
        if billed is None or lower is None:
            return "the file does not publish both the billed level and a lower level"
        low_price = lower["discounted_cash"] if cash_basis else lower["gross"]
        billed_price = _basis_price(ln, cash_basis)
        if billed_price is None or low_price is None:
            return "the bill and the file do not both price this visit on the same basis"
        return Finding(kind="upcoded", line_nos=nos, evidence=[_to_row(billed), _to_row(lower)], basis=basis,
                       needs_user_judgment=True, confidence=c.confidence,
                       amount_at_stake=round(billed_price - low_price, 2),
                       summary=c.summary or (f"Line {ln.line_no} bills visit level {ln.code}, but the printed visit "
                                             f"summary reads like a lower level such as {lower['code']}."))

    if c.kind == "out_of_network":
        if len(sel) != 1:
            return "an out-of-network finding must name one line"
        ln = sel[0]
        if not (ln.network_note and OON_NOTE.search(ln.network_note)):
            return "the line carries no out-of-network note on the bill"
        if cash_basis:
            return "a self-pay bill has no plan network"
        return Finding(kind="out_of_network", line_nos=nos, evidence=[], basis="gross",
                       amount_at_stake=round(_price(ln) or 0.0, 2), regulation=NSA_CITE,
                       confidence=c.confidence,
                       summary=c.summary or (f"Line {ln.line_no} is billed by {ln.provider or 'an out-of-network provider'} "
                                             f"at an in-network facility; the balance is protected."))

    return f"{c.kind} is not a kind this validator can confirm"


# ---- the audit ------------------------------------------------------------

async def audit_async(bill: Bill, bill_id: str, progress: Optional[Any] = None,
                      timeline: Optional[list] = None) -> AuditResult:
    """Audit one bill: resolve the hospital, run the specialist Graph, validate.

    progress: optional asyncio.Queue that receives {"specialist": name, "status": "done"}
    as each specialist lands, so a caller can stream them. timeline: optional list that
    collects per-node start/end timestamps (proof the Graph nodes really overlap)."""
    h = resolve(f"{bill.hospital_name} {bill.hospital_address or ''}")
    notes: list[str] = []
    if not h.get("found"):
        return AuditResult(
            bill_id=bill_id, hospital_id=None, findings=[], clean=False,
            status="unsupported_hospital",
            notes=[f"'{bill.hospital_name}' is not a hospital Fairbill covers, so this bill was "
                   f"not audited; no price file to check it against"])
    hospital_id = h["hospital_id"]
    context = build_context(bill, hospital_id)

    collector: dict[str, Any] = {"claims": [], "findings": [], "notes": notes,
                                 "validated": False, "ran": set(), "progress": progress,
                                 "timeline": timeline if timeline is not None else []}
    b = GraphBuilder()
    b.add_node(ContextNode(), "context")
    for name in SPECIALISTS:
        b.add_node(SpecialistNode(name, collector), name)
        b.add_edge("context", name)
    b.add_node(ValidatorNode(collector), "validator")
    for name in SPECIALISTS:
        b.add_edge(name, "validator", condition=all_dependencies_complete(list(SPECIALISTS)))
    b.set_entry_point("context")
    b.set_node_timeout(120)
    b.set_execution_timeout(300)
    graph = b.build()

    state = {"bill": bill, "hospital_id": hospital_id, "context": context}
    try:
        await graph.invoke_async(f"Audit bill {bill_id}.", invocation_state=state)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"graph execution failed ({type(exc).__name__}: {exc})")
    if not collector["validated"] and collector["claims"]:
        # the validator node did not run (graph error); validate here instead
        kept, vnotes = validate(collector["claims"], bill, hospital_id)
        collector["findings"] = kept
        notes.extend(vnotes)

    failed = [n for n in SPECIALISTS if n not in collector["ran"]]
    if failed:
        notes.append(f"incomplete audit: {', '.join(failed)} did not finish, so part of this bill "
                     f"was not checked")
    status = "incomplete" if failed else "audited"

    first = next((ln for ln in bill.lines if ln.code), None)
    row = _row_for_line(hospital_id, first.code, first.charge) if first else None
    return AuditResult(bill_id=bill_id, hospital_id=hospital_id, findings=collector["findings"],
                       clean=status == "audited" and not collector["findings"], status=status,
                       file_used=_to_row(row) if row else None, notes=notes)


def audit(bill: Bill, bill_id: str) -> AuditResult:
    """Sync wrapper around audit_async. Never call it from inside a running event loop."""
    return asyncio.run(audit_async(bill, bill_id))


# ---- gallery bills --------------------------------------------------------

TRUTH_PATH = REPO_ROOT / "gallery" / "truth.json"

# (bill_id, hospital_name, patient, payer, visit_type, dates, clinical_note,
#  lines as (line_no, code, description, charge, patient_price))
_NORMAN = "Norman Regional Health System"
_OU = "OU Health - University of Oklahoma Medical Center"

SPEC_BILLS: list[dict] = [
    {"id": "bill_01", "hospital": _NORMAN, "patient": "Jordan Sample", "payer": "Self-pay",
     "visit_type": "Emergency", "date": "2026-08-14", "note": None,
     "lines": [(1, "99283", "ED Services Level 3", 1729.00, 1037.40),
               (2, "36415", "ED Venipuncture, Routine", 44.00, 26.40),
               (3, "85025", "Complete Blood Count (CBC)", 140.00, 84.00),
               (4, "85025", "Complete Blood Count (CBC)", 140.00, 84.00),
               (5, "80053", "Comprehensive Metabolic Panel", 275.00, 165.00),
               (6, "71046", "Chest X-ray, 2 views", 449.00, 269.40),
               (7, "J1885", "Ketorolac 15 mg injection", 48.82, 29.29),
               (8, "96372", "Therapeutic injection, IM", 221.00, 132.60)],
     "planted": [("duplicate", [3, 4])]},
    {"id": "bill_02", "hospital": _NORMAN, "patient": "Casey Example", "payer": "Self-pay",
     "visit_type": "Outpatient diagnostics", "date": "2026-07-22", "note": None,
     "lines": [(1, "36415", "Venipuncture", 46.00, 27.60),
               (2, "84443", "Thyroid Stimulating Hormone", 248.00, 148.80),
               (3, "83036", "Hemoglobin A1c", 135.00, 81.00),
               (4, "93005", "ECG tracing, 12 lead", 409.00, 245.40),
               (5, "71046", "Chest X-ray, 2 views", 449.00, 449.00)],
     "planted": [("above_cash", [5])]},
    {"id": "bill_03", "hospital": _OU, "patient": "Riley Demo", "payer": "Self-pay",
     "visit_type": "Outpatient lab", "date": "2026-08-03", "note": None,
     "lines": [(1, "36415", "Collection of venous blood, venipuncture", 90.00, 9.00),
               (2, "85027", "Complete blood cell count, automated", 257.00, 25.70),
               (3, "85018", "Hemoglobin", 113.00, 11.30),
               (4, "85014", "Red blood cell concentration (hematocrit)", 123.00, 12.30),
               (5, "85007", "Microscopic exam, white cells, manual differential", 636.00, 63.60),
               (6, "80053", "Blood test, comprehensive group of chemicals", 1025.00, 102.50)],
     "planted": [("unbundled", [2, 3, 4, 5])]},
    {"id": "bill_04", "hospital": _OU, "patient": "Morgan Placeholder", "payer": "Self-pay",
     "visit_type": "Emergency", "date": "2026-08-21",
     "note": ("Reason for visit: sore throat and cough, 2 days. Vitals normal, no fever. "
              "Rapid strep negative. Chest X-ray clear. Discharged home with instructions; "
              "no prescriptions."),
     "lines": [(1, "99285", "Emergency department visit, life-threatening or high severity", 6795.00, 679.50),
               (2, "36415", "Collection of venous blood, venipuncture", 90.00, 9.00),
               (3, "80053", "Blood test, comprehensive group of chemicals", 1025.00, 102.50),
               (4, "71046", "X-ray of chest, 2 views", 683.00, 68.30),
               (5, "93005", "Routine electrocardiogram, 12 leads, tracing", 532.00, 53.20),
               (6, "J7030", "Normal saline solution infusion", 7.00, 0.70)],
     "planted": [("upcoded", [1])]},
    {"id": "bill_05", "hospital": _NORMAN, "patient": "Taylor Fictional",
     "payer": "Sooner Plains Health Plan (fictional)", "visit_type": "Outpatient procedure",
     "date": "2026-06-10", "note": None,
     "lines": [(1, "45378", "Colonoscopy, diagnostic", 2506.00, 360.00),
               (2, "J7030", "Sodium chloride 1000 mL bag", 86.66, 12.00),
               (3, "00812", "Anesthesia for screening colonoscopy", 1850.00, 1850.00,
                "Red River Anesthesia Associates (fictional)",
                "OUT OF NETWORK: this provider is not contracted with your plan")],
     "planted": [("out_of_network", [3])]},
    {"id": "bill_06", "hospital": _NORMAN, "patient": "Alex Specimen", "payer": "Self-pay",
     "visit_type": "Outpatient lab", "date": "2026-07-08", "note": None,
     "lines": [(1, "36415", "Venipuncture", 46.00, 27.60),
               (2, "85025", "Complete Blood Count (CBC)", 140.00, 84.00),
               (3, "80053", "Comprehensive Metabolic Panel", 275.00, 165.00),
               (4, "84443", "Thyroid Stimulating Hormone", 248.00, 148.80),
               (5, "81001", "Urinalysis, automated with microscopy", 164.00, 98.40)],
     "planted": []},
]


def _spec_bill(spec: dict) -> Bill:
    lines = []
    for row in spec["lines"]:
        no, code, desc, charge, price = row[:5]
        provider = row[5] if len(row) > 5 else None
        note = row[6] if len(row) > 6 else None
        lines.append(BillLine(line_no=no, code=code, code_type="HCPCS" if code[0].isalpha() else "CPT",
                              description=desc, charge=charge, patient_price=price,
                              date_of_service=spec["date"], provider=provider, network_note=note))
    return Bill(hospital_name=spec["hospital"], patient_name=spec["patient"], payer=spec["payer"],
                visit_type=spec["visit_type"], clinical_note=spec["note"],
                service_date_start=spec["date"], service_date_end=spec["date"],
                lines=lines, fictional_label_present=True)


def gallery_bills() -> list[dict]:
    """Bench cases: gallery/truth.json when it exists, else the SPEC tables above."""
    if TRUTH_PATH.exists():
        try:
            doc = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
            entries = doc if isinstance(doc, list) else doc.get("bills", doc.get("entries", []))
            out = []
            for e in entries:
                out.append({"id": e["id"], "bill": Bill(**e["bill"]),
                            "planted": [(f["kind"], sorted(f["line_nos"])) for f in e.get("planted", [])],
                            "source": "truth.json"})
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            print(f"# truth.json unusable ({type(exc).__name__}: {exc}); falling back to SPEC tables")
    return [{"id": s["id"], "bill": _spec_bill(s),
             "planted": [(k, sorted(n)) for k, n in s["planted"]], "source": "SPEC.md"}
            for s in SPEC_BILLS]


# ---- CLI ------------------------------------------------------------------

def _print_findings(res: AuditResult, verbose: bool = False) -> None:
    if not res.findings:
        print("No problems found. Every line matches the hospital's posted prices.")
    for f in res.findings:
        print(f"\n* {f.kind.replace('_', ' ').upper()} on line(s) {', '.join(str(n) for n in f.line_nos)}")
        print(f"  {f.summary}")
        if f.amount_at_stake is not None:
            print(f"  At stake: ${f.amount_at_stake:,.2f} ({f.basis} price)")
        if f.regulation:
            print(f"  Protection: {f.regulation}")
        if f.needs_user_judgment:
            print("  Needs your judgment: only your medical record settles this one.")
        for ev in f.evidence:
            print(f"  File: {ev.code} line {ev.source_line}, posted gross ${ev.gross:,.2f}, "
                  f"cash ${ev.discounted_cash:,.2f} ({ev.fetched_on})")
    if verbose:
        for n in res.notes:
            print(f"  note: {n}")


def run_bench() -> int:
    cases = gallery_bills()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    rows, results = [], []
    planted_total = matched_total = false_total = 0
    for case in cases:
        t0 = time.time()
        res = audit(case["bill"], case["id"])
        dt = time.time() - t0
        planted = case["planted"]
        found = [(f.kind, sorted(f.line_nos)) for f in res.findings]
        matched = [p for p in planted if p in found]
        false_flags = [f for f in found if f not in planted]
        planted_total += len(planted)
        matched_total += len(matched)
        false_total += len(false_flags)
        rows.append({
            "bill": case["id"],
            "planted": ", ".join(k for k, _ in planted) or "clean",
            "found": ", ".join(f"{k}{n}" for k, n in found) or "-",
            "matched": f"{len(matched)}/{len(planted)}" if planted else "n/a",
            "false": str(len(false_flags)),
            "sec": f"{dt:.1f}",
        })
        results.append({"bill_id": case["id"], "source": case["source"], "seconds": round(dt, 2),
                        "planted": [[k, n] for k, n in planted], "found": [[k, n] for k, n in found],
                        "matched": len(matched), "false_flags": len(false_flags),
                        "result": res.model_dump()})
        print(f"  {case['id']}: {len(matched)}/{len(planted)} matched, {len(false_flags)} false, {dt:.1f}s")

    hdr = ["bill", "planted", "found", "matched", "false", "sec"]
    widths = {h: max(len(h), *(len(r[h]) for r in rows)) for h in hdr}
    print()
    print(" | ".join(h.ljust(widths[h]) for h in hdr))
    print("-|-".join("-" * widths[h] for h in hdr))
    for r in rows:
        print(" | ".join(r[h].ljust(widths[h]) for h in hdr))
    clean_case = next((r for r in results if not r["planted"]), None)
    clean_flags = "n/a" if clean_case is None else clean_case["false_flags"]
    print(f"\nfound {matched_total} of {planted_total} planted, {false_total} false flags "
          f"({clean_flags} on the clean bill)")
    out = RUN_DIR / "bench.json"
    out.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "source": cases[0]["source"],
                               "totals": {"planted": planted_total, "matched": matched_total,
                                          "false_flags": false_total},
                               "bills": results}, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return 0 if matched_total == planted_total and false_total == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fairbill.audit")
    ap.add_argument("--bench", action="store_true", help="audit every gallery bill and score it")
    ap.add_argument("--bill", help="audit one gallery bill by id, e.g. bill_01")
    ap.add_argument("--verbose", action="store_true", help="also print validator notes")
    ns = ap.parse_args(argv)
    if ns.bench:
        return run_bench()
    if ns.bill:
        case = next((c for c in gallery_bills() if c["id"] == ns.bill), None)
        if case is None:
            print(f"unknown bill '{ns.bill}'")
            return 2
        t0 = time.time()
        res = audit(case["bill"], case["id"])
        print(f"{case['id']}: {case['bill'].hospital_name} / {case['bill'].patient_name} "
              f"({time.time() - t0:.1f}s)\n")
        _print_findings(res, verbose=ns.verbose)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
