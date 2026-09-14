"""Draft the letter the patient can print and mail.

The model writes the prose; Python owns every fact. Citations, addresses and the
footer are filled in from the registry and the audit, and a deterministic
post-check rejects any amount or billing code in the body that did not come from
the inputs. A letter that fails the check is redrafted, then redrafted on the
same-bar fallback model, and only then does the draft fail loudly.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path
from typing import Optional

from strands import Agent

from fairbill.config import down_reason, live_ladder, make_model, mark_down, model_name, retry_strategy
from fairbill.schema import AuditResult, Bill, DecisionCard, FileRow, Letter
from fairbill.tools import hospital_record

FOOTER = ("Fairbill drafts letters from public price data. This is not legal advice. "
          "Patient names in this demo are fictional.")

CFR_180 = "45 CFR 180.50"
CFR_149 = "45 CFR 149.420"

SOURCE_TEXT = {
    CFR_180: ("45 CFR 180.50 requires this hospital to publish its standard charges, "
              "including the discounted cash price, in a machine-readable file."),
    CFR_149: ("45 CFR 149.420 (No Surprises Act) limits a nonparticipating provider at a "
              "participating facility to the in-network cost-sharing amount."),
}
CFR_URL = {
    CFR_180: "https://www.ecfr.gov/current/title-45/part-180",
    CFR_149: "https://www.ecfr.gov/current/title-45/part-149",
}

KIND_BY_OPTION = {
    "dispute": "dispute",
    "ask_provider": "dispute",
    "itemized": "itemized_request",
}

SYSTEM_PROMPT = """You write one short letter from a patient to a hospital's billing office.

Rules, without exception:
- Use only the facts in the input block. Never invent or compute an amount, a billing
  code, a date, a regulation, or a right that is not printed there.
- Plain language a patient would say out loud. No legal threats, no medical advice.
- Under 350 words. No markdown, no bullet characters other than "-", no signature block
  beyond "Sincerely," and the patient's name.
- Quote the hospital's own price-file row (code, posted price, file line) and the
  regulation strings exactly as the input block spells them.
- On a self-pay bill quote the "your price" (cash) figures for every line you name and for
  the amount at stake; name the gross charge only as "list charge before discount".
- Say plainly what is asked for and by when. Use the "Respond by" date exactly as given;
  never write a date that has already passed.
- Do not repeat the hospital's street address, city, state or ZIP in the body; it is
  printed above the letter already.
- Plain ASCII punctuation only: no em dashes, no smart quotes.
Return the structured Letter: re_line is one line starting with "Re:", body is the letter
text from the salutation to the patient's name."""


# ---- what the letter is allowed to say ------------------------------------

_MONEY = re.compile(r"\$\s*[\d,]+(?:\.\d+)?|(?<![\d.])\d[\d,]*\.\d{2}(?![\d])"
                    r"|\b\d[\d,]*\s*(?:dollars|USD)\b"
                    # bare integers too: an invented "9999" or "7,432" is still a dollar claim
                    r"|(?<![\d.,$/-])\d{1,3}(?:,\d{3})+(?![\d.,/-])|(?<![\d.,$/-])\d{3,4}(?![\d.,/-])")
_CODE = re.compile(r"\b[A-Z]\d{4}\b|\b\d{5}\b")
WORD_LIMIT = 350


def _plain(text: str) -> str:
    """Patient-facing copy stays plain ASCII: no dashes standing in for punctuation."""
    out = (text or "").replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    out = out.replace("’", "'").replace("‘", "'")
    return out.replace("“", '"').replace("”", '"')


def _amt(tok: str) -> str:
    raw = re.sub(r"(?i)\s*(dollars|usd)\s*$", "", tok.replace("$", "").replace(",", "").strip())
    return f"{float(raw):.2f}"


def _m(v: Optional[float]) -> str:
    return "not posted" if v is None else f"${v:,.2f}"


def _rows(result: AuditResult) -> list[FileRow]:
    rows = [ev for f in result.findings for ev in f.evidence]
    if result.file_used and not rows:
        rows = [result.file_used]
    return rows


def allowed_facts(bill: Bill, result: AuditResult, card: DecisionCard) -> tuple[set[str], set[str]]:
    """(allowed amounts as '0.00' strings, allowed code-like tokens)."""
    amounts: set[str] = set()
    codes: set[str] = set()

    def money(v: Optional[float]) -> None:
        if v is not None:
            amounts.add(f"{float(v):.2f}")

    for ln in bill.lines:
        money(ln.charge)
        money(ln.patient_price)
        if ln.code:
            codes.add(ln.code.upper())
    money(bill.total_charges)
    money(bill.total_adjustments)
    money(bill.patient_balance)
    money(card.amount_at_stake)
    for f in result.findings:
        money(f.amount_at_stake)
        # the finding's lines added up (a duplicate billed twice, an unbundled panel) is
        # arithmetic on the bill, not a new claim
        hit = [ln for ln in bill.lines if ln.line_no in (f.line_nos or [])]
        if len(hit) > 1:
            money(sum(ln.charge or 0 for ln in hit))
            money(sum(ln.patient_price or 0 for ln in hit if ln.patient_price is not None))
    for row in _rows(result):
        money(row.gross)
        money(row.discounted_cash)
        codes.add(row.code.upper())
        codes.add(str(row.source_line))
    for n in card.evidence_lines:
        codes.add(str(n))
    # the account number and the CFR citations are identifiers, not claims about money
    for tok in re.findall(r"\d[\d.,]*", bill.account_number or ""):
        codes.add(tok.strip(".,"))
    for cite in (CFR_180, CFR_149):
        for tok in re.findall(r"\d[\d.,]*", cite):
            codes.add(tok.strip(".,"))
            amounts.add(tok.strip(".,"))
    return amounts, codes


def _cash_basis_problems(bill: Bill, result: AuditResult, quoted: set[str]) -> list[str]:
    """A cash-basis finding may not be argued with the gross charge alone."""
    lines = {ln.line_no: ln for ln in bill.lines}
    out = []
    for f in result.findings:
        if f.basis != "cash":
            continue
        for n in f.line_nos:
            ln = lines.get(n)
            if ln is None or ln.patient_price is None or ln.charge is None:
                continue
            if f"{ln.charge:.2f}" in quoted and f"{ln.patient_price:.2f}" not in quoted:
                out.append(f"line {n} quotes the gross charge {ln.charge:,.2f} on a cash-price "
                           f"finding without the cash price {ln.patient_price:,.2f}")
    return out


def postcheck(letter: Letter, bill: Bill, result: AuditResult, card: DecisionCard,
              sources: str = "") -> list[str]:
    """Every amount and code in the body must come from the inputs; every citation
    string must appear in the body. Returns the list of problems (empty = good).

    The body is checked with the Sources block attached, since that is what the patient
    mails; the numbers the Sources block itself prints are allowed by construction.
    """
    amounts, codes = allowed_facts(bill, result, card)
    body = letter.body
    prose = body[: -len(sources)].rstrip() if sources and body.endswith(sources) else body
    if sources:
        for tok in _MONEY.findall(sources):
            amounts.add(_amt(tok))
        for tok in _CODE.findall(sources):
            codes.add(tok.upper())
    problems = []
    # what the patient's own prose quotes; the Sources block prints both prices by design
    quoted = {_amt(tok) for tok in _MONEY.findall(prose)}
    for tok in _MONEY.findall(body):
        if _amt(tok) not in amounts and tok.strip() not in codes:  # a bare row or code number is not money
            problems.append(f"amount {tok.strip()} is not in the inputs")
    for tok in _CODE.findall(body):
        if tok.upper() not in codes:
            problems.append(f"code {tok} is not in the inputs")
    for cite in letter.citations:
        if cite not in body:
            problems.append(f"citation missing from the body: {cite}")
    words = len(prose.split())
    if words > WORD_LIMIT:
        problems.append(f"body is {words} words, over the {WORD_LIMIT}-word limit")
    if letter.kind in ("dispute", "nsa_complaint"):
        problems.extend(_cash_basis_problems(bill, result, quoted))
    return problems


# ---- citations Python owns -------------------------------------------------

def build_citations(result: AuditResult, card: DecisionCard, kind: str) -> tuple[list[str], str]:
    """(short citation strings that must appear in the body, the Sources block)."""
    cites: list[str] = []
    sources: list[str] = []
    rows = _rows(result)
    row = rows[0] if rows else None
    if row is not None:
        tag = f"row {row.source_line}"
        cites.append(tag)
        sources.append(f"- The hospital's own standard-charges file, {tag}: {row.code} "
                       f"{row.description}, posted gross {_m(row.gross)}, discounted cash price "
                       f"{_m(row.discounted_cash)} (file fetched {row.fetched_on}). {row.source_url}")
    cites.append(CFR_180)
    sources.append(f"- {SOURCE_TEXT[CFR_180]} {CFR_URL[CFR_180]}")
    if kind == "nsa_complaint":
        cites.append(CFR_149)
        sources.append(f"- {SOURCE_TEXT[CFR_149]} {CFR_URL[CFR_149]}")
    if kind == "itemized_request":
        cites.append("itemized statement")
    return cites, "Sources:\n" + "\n".join(sources)


def letter_kind(card: DecisionCard, option_id: str) -> str:
    if option_id == "dispute" and card.kind == "out_of_network":
        return "nsa_complaint"
    return KIND_BY_OPTION.get(option_id, "dispute")


def _facts_block(bill: Bill, result: AuditResult, card: DecisionCard, kind: str,
                 cites: list[str], to_name: str, to_address: str) -> str:
    out = [
        f"Letter kind: {kind}",
        f"Hospital: {to_name}",
        f"Billing address: {to_address}",
        f"Patient (fictional demo name): {bill.patient_name}",
        f"Account number: {bill.account_number or 'not printed'}",
        f"Statement date: {bill.statement_date or 'not printed'}",
        f"Payer: {bill.payer}",
        f"Respond by: {card.deadline.isoformat()} ({card.deadline_reason})",
        f"Amount in question: ${card.amount_at_stake:,.2f}",
        "",
        "What the audit found:",
        card.situation,
        "",
        "Bill lines involved:",
    ]
    involved = {n for f in result.findings for n in f.line_nos}
    for ln in bill.lines:
        if ln.line_no in involved or not involved:
            out.append(f"  line {ln.line_no}: {ln.code or '-'} {ln.description}, "
                       f"charge {_m(ln.charge)}"
                       + (f", your price {_m(ln.patient_price)}" if ln.patient_price is not None else ""))
    out.append("")
    out.append("The hospital's own price file rows:")
    for row in _rows(result):
        out.append(f"  {row.code} {row.description}: posted gross {_m(row.gross)}, "
                   f"discounted cash {_m(row.discounted_cash)}, file line {row.source_line}")
    out.append("")
    out.append("Strings you must use exactly, at least once each: " + "; ".join(cites))
    if kind == "itemized_request":
        out.append("Ask for a fully itemized statement of every line, code and charge before payment.")
    if kind == "nsa_complaint":
        out.append("State that the patient owes only the in-network cost sharing for this line.")
    if card.kind == "upcoded":
        out.append("Ask the hospital to justify the visit level against the visit summary; "
                   "do not assert that the level is wrong.")
    return "\n".join(out)


async def draft_letter_async(bill: Bill, result: AuditResult, card: DecisionCard,
                             option_id: str, model=None) -> Letter:
    kind = letter_kind(card, option_id)
    rec = hospital_record(result.hospital_id) if result.hospital_id else None
    to_name = _plain((rec or {}).get("name") or bill.hospital_name)
    to_address = _plain((rec or {}).get("billing_address") or bill.hospital_address or to_name)
    cites, sources = build_citations(result, card, kind)
    prompt = _facts_block(bill, result, card, kind, cites, to_name, to_address)

    problems: list[str] = []
    notes: list[str] = []
    attempts = [(model, "given model")] if model is not None else []
    # Retry budget: the primary gets three whole drafts (given model, primary, primary retry),
    # each with 3 model attempts at 2 s then 4 s of backoff, so about 3 x (6 s of waiting plus
    # roughly 9 s of inference, measured 2026-09-14) before Haiku is reached. A single
    # ThrottlingException costs one retry inside the first draft and never the fallback model.
    rungs = live_ladder()
    attempts += [(rungs[0], "primary"), (rungs[0], "primary retry")]
    attempts += [(r, model_name(r)) for r in rungs[1:]]
    for spec, label in attempts:
        if isinstance(spec, str) and spec != rungs[0] and down_reason(spec):
            continue  # this rung was marked down by an earlier attempt
        mdl = make_model(model=spec) if isinstance(spec, str) else spec
        agent = Agent(model=mdl, system_prompt=SYSTEM_PROMPT,
                      structured_output_model=Letter, callback_handler=None,
                      retry_strategy=retry_strategy())
        try:
            res = await agent.invoke_async(prompt)
            draft = res.structured_output
        except Exception as exc:  # noqa: BLE001 - try the next model before failing
            problems = [f"{type(exc).__name__}: {exc}"]
            notes.append(f"letter attempt on the {label} model failed: {type(exc).__name__}")
            if isinstance(spec, str) and mark_down(spec, exc):
                attempts = [a for a in attempts if a[0] != spec]  # no second try on a throttled rung
            continue
        if draft is None:
            problems = ["model returned no structured letter"]
            notes.append(f"letter attempt on the {label} model returned no structured letter")
            continue
        if isinstance(spec, str) and spec != rungs[0]:
            # a step down the ladder is never silent: the note rides along in the letter payload
            why = down_reason(rungs[0]) or "failed"
            notes.append(f"{model_name(rungs[0])} {why}; letter drafted with {model_name(spec)}")
        letter = Letter(
            bill_id=card.bill_id, kind=kind, to_name=to_name, to_address=to_address,
            from_name=bill.patient_name,
            re_line=_plain(draft.re_line) or f"Re: account {bill.account_number or card.bill_id}",
            body=f"{_plain(draft.body).strip()}\n\n{sources}", citations=cites, footer=FOOTER,
            notes=list(notes))
        problems = postcheck(letter, bill, result, card, sources=sources)
        if not problems:
            return letter
        notes.append(f"draft on the {label} model failed the post-check "
                     f"({len(problems)} problem(s)); redrafted")
    raise ValueError(f"letter post-check failed after every attempt: {problems}")


def draft_letter(bill: Bill, result: AuditResult, card: DecisionCard,
                 option_id: str, model=None) -> Letter:
    """Sync wrapper. Inside a running event loop use draft_letter_async."""
    return asyncio.run(draft_letter_async(bill, result, card, option_id, model))


def render(letter: Letter, today: Optional[date] = None) -> str:
    """Plain-text render, ready to print."""
    return "\n".join([
        max(today or date.today(), date.today()).isoformat(), "",  # never a past date
        letter.to_name, letter.to_address, "",
        letter.re_line, "",
        letter.body, "",
        letter.footer, ""])


def save(letter: Letter, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{letter.bill_id}_{letter.kind}.txt"
    path.write_text(render(letter), encoding="utf-8")
    return path
