"""Decision Card, calendar entry, and the end-to-end pipeline.

The card is deterministic Python: the model already did its work in the audit,
and what the patient is asked to decide must not change between two runs of the
same bill. One card per bill, two or three options, one default, one deadline,
one evidence link.

    python -m fairbill.decisions --bill bill_02 [--no-fetch]
    python -m fairbill.decisions --guard-demo
    python -m fairbill.decisions --precompute
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fairbill import ledger
from fairbill.schema import AuditResult, Bill, DecisionCard, Finding, Letter, Option
from fairbill.tools import hospital_record

REPO_ROOT = Path(__file__).resolve().parents[2]
GALLERY_DIR = REPO_ROOT / "gallery"
RESULTS_DIR = GALLERY_DIR / "results"
TRUTH_PATH = GALLERY_DIR / "truth.json"

PAY_DUE_REASON = ("Payment is due on this date; a written dispute before it keeps the account "
                  "out of collections")
CMS_REASON = "CMS patient-provider dispute window: 120 calendar days from the bill date"
DUE_REASON_CLEAN = "Payment due date printed on the statement"

NOT_CHECKED = {
    "unsupported_hospital": (
        "Fairbill could not check this bill: this hospital's published price file is not in the "
        "gallery yet. You can still ask for an itemized bill."),
    "incomplete": (
        "Fairbill could not finish checking this bill against the hospital's price file, so no "
        "verdict is given. Run it again, or ask for an itemized bill."),
}

OPTIONS = {
    "dispute": Option(id="dispute", label="Send the dispute letter",
                      consequence="Letter cites the hospital's own price file row; hospital has 30 days to answer"),
    "itemized": Option(id="itemized", label="Request the itemized bill first",
                       consequence="Delays payment; confirms each line before disputing"),
    "drop": Option(id="drop", label="Pay as billed", consequence="You accept the charge"),
    "close": Option(id="drop", label="Close the case", consequence="Nothing is sent; the check is logged"),
    "ask_provider": Option(id="ask_provider", label="Ask the hospital to justify the visit level",
                           consequence="Letter asks for the coding rationale against your visit summary"),
    "nsa": Option(id="dispute", label="Send the No Surprises Act notice",
                  consequence="Cites 45 CFR 149.420; provider may bill only in-network cost sharing"),
    "pay_in_network": Option(id="pay_in_network", label="Pay only the in-network share",
                             consequence="Pay what the plan says you owe for in-network care; keep the letter on file"),
}


def _money(v: Optional[float]) -> str:
    return "-" if v is None else f"${v:,.2f}"


def _lines_phrase(nos: list[int]) -> str:
    s = [str(n) for n in nos]
    if len(s) == 1:
        return f"Line {s[0]}"
    if len(s) == 2:
        return f"Line {s[0]} and line {s[1]}"
    return "Lines " + ", ".join(s[:-1]) + f" and {s[-1]}"


def _line_map(bill: Bill) -> dict[int, Any]:
    return {ln.line_no: ln for ln in bill.lines}


def lead_finding(result: AuditResult) -> Optional[Finding]:
    """The finding the card leads with: most money first, then the earliest line."""
    if not result.findings:
        return None
    return sorted(result.findings,
                  key=lambda f: (-(f.amount_at_stake or 0.0), min(f.line_nos or [0]), f.kind))[0]


def _situation(bill: Bill, f: Finding) -> str:
    lines = _line_map(bill)
    first = lines.get(min(f.line_nos)) if f.line_nos else None
    code = (first.code if first else None) or "the code"
    ev = f.evidence[0] if f.evidence else None
    row = f" (row {ev.source_line})" if ev else ""
    amt = _money(f.amount_at_stake)
    if f.kind == "duplicate":
        charge = _money(first.charge if first else None)
        when = (first.date_of_service if first else None) or bill.service_date_start or "the visit date"
        posted = _money(ev.gross if ev else None)
        return (f"{_lines_phrase(f.line_nos)} bill {code} twice on {when} at {charge}. "
                f"The hospital's price file lists it once at {posted}{row}, so {amt} is billed twice.")
    if f.kind == "above_cash":
        charge = _money(first.patient_price if first and first.patient_price is not None
                        else (first.charge if first else None))
        cash = _money(ev.discounted_cash if ev else None)
        return (f"{_lines_phrase(f.line_nos)} prices {code} at {charge}, above the {cash} cash price the "
                f"hospital publishes for it{row}, a difference of {amt}.")
    if f.kind == "unbundled":
        codes = ", ".join(sorted({(lines[n].code or "?") for n in f.line_nos if n in lines}))
        return (f"{_lines_phrase(f.line_nos)} bill {codes} separately although the hospital's own file "
                f"prices them as one panel{row}, {amt} more than the panel price.")
    if f.kind == "upcoded":
        charge = _money(first.charge if first else None)
        return (f"{_lines_phrase(f.line_nos)} bills visit level {code} at {charge}, {amt} more than the "
                f"next level down{row}. "
                "This needs your judgment: does the visit summary match what happened?")
    if f.kind == "out_of_network":
        desc = first.description if first else "this service"
        return (f"{_lines_phrase(f.line_nos)} ({desc}) was billed by an out-of-network provider at an "
                f"in-network facility, leaving {amt} the No Surprises Act says you should not owe.")
    return f"{_lines_phrase(f.line_nos)} is flagged: {f.summary}"


def _also(result: AuditResult, lead: Finding) -> str:
    others = [f for f in result.findings if f is not lead]
    if not others:
        return ""
    parts = [f"{f.kind.replace('_', ' ')} on {_lines_phrase(f.line_nos).lower()} ({_money(f.amount_at_stake)})"
             for f in sorted(others, key=lambda f: min(f.line_nos or [0]))]
    return "Also flagged: " + "; ".join(parts) + "."


PAST_DUE_NOTE = (" (the statement's due date, {due}, has passed; act today and ask billing to "
                 "pause collection while the dispute is open)")


def _due_date(bill: Bill, today: date) -> tuple[date, str]:
    """The response deadline, never in the past, and the note that explains where it came from."""
    base = bill.statement_date or bill.service_date_end or bill.service_date_start
    due = today + timedelta(days=30)
    note = " (no statement date was read; today used)"
    if base:
        try:
            due = date.fromisoformat(base[:10]) + timedelta(days=30)
            note = "" if bill.statement_date else " (no statement date was read; service date used)"
        except ValueError:
            due = today + timedelta(days=30)
            note = " (the date printed on the statement could not be read; today used)"
    if due < today:
        return today, PAST_DUE_NOTE.format(due=due.isoformat())
    return due, note


def _deadline_reason(base_reason: str, note: str) -> str:
    """A past-due note replaces the reason instead of contradicting it."""
    if note.startswith(" (the statement's due date"):
        text = note.strip()[1:-1]
        return text[0].upper() + text[1:]
    return base_reason + note


def _evidence(result: AuditResult, lead: Optional[Finding]) -> tuple[str, list[int]]:
    rec = hospital_record(result.hospital_id) if result.hospital_id else None
    url = (rec or {}).get("mrf_url") or (rec or {}).get("transparency_page") or ""
    out: list[int] = []
    for f in ([lead] if lead else result.findings):
        for ev in (f.evidence if f else []):
            if ev.source_line not in out:
                out.append(ev.source_line)
    if not out and result.file_used:
        out = [result.file_used.source_line]
    return url, out


def build_card(bill: Bill, result: AuditResult, bill_id: str, today: date) -> DecisionCard:
    """One card: what happened, what can be done, what happens by default, by when."""
    lead = lead_finding(result)
    url, ev_lines = _evidence(result, lead)
    due, due_note = _due_date(bill, today)

    if result.status != "audited" or (not result.clean and lead is None):
        # Nothing was checked against the hospital's file, so the card may not claim a verdict.
        status = result.status if result.status != "audited" else "incomplete"
        return DecisionCard(
            bill_id=bill_id, situation=NOT_CHECKED[status], status=status,
            options=[OPTIONS["itemized"], OPTIONS["drop"]], default_option="itemized",
            deadline=due, deadline_reason=_deadline_reason(DUE_REASON_CLEAN, due_note),
            evidence_url=url, evidence_lines=ev_lines, amount_at_stake=0.0, basis="unknown",
            needs_user_judgment=False, clean=False, kind=None)

    if lead is None:
        return DecisionCard(
            bill_id=bill_id,
            situation=("This bill matches the hospital's published prices line by line. "
                       "Nothing to dispute."),
            status="audited",
            options=[OPTIONS["close"]], default_option="drop",
            deadline=due, deadline_reason=_deadline_reason(DUE_REASON_CLEAN, due_note),
            evidence_url=url, evidence_lines=ev_lines, amount_at_stake=0.0, basis="unknown",
            needs_user_judgment=False, clean=True, kind=None)

    if lead.kind == "out_of_network":
        options = [OPTIONS["nsa"], OPTIONS["pay_in_network"], OPTIONS["drop"]]
        default = "dispute"
        deadline, reason = today + timedelta(days=120), _deadline_reason(CMS_REASON, due_note)
    elif lead.kind == "upcoded":
        options = [OPTIONS["ask_provider"], OPTIONS["itemized"], OPTIONS["drop"]]
        default = "ask_provider"
        deadline, reason = due, _deadline_reason(PAY_DUE_REASON, due_note)
    else:
        options = [OPTIONS["dispute"], OPTIONS["itemized"], OPTIONS["drop"]]
        default = "itemized" if lead.kind == "unbundled" else "dispute"
        deadline, reason = due, _deadline_reason(PAY_DUE_REASON, due_note)

    situation = _situation(bill, lead)
    also = _also(result, lead)
    if also:
        situation = f"{situation} {also}"
    return DecisionCard(
        bill_id=bill_id, situation=situation, status="audited",
        options=options, default_option=default,
        deadline=deadline, deadline_reason=reason, evidence_url=url, evidence_lines=ev_lines,
        amount_at_stake=round(lead.amount_at_stake or 0.0, 2), basis=lead.basis,
        needs_user_judgment=lead.needs_user_judgment, clean=False, kind=lead.kind)


# ---- calendar -------------------------------------------------------------

def _fold(line: str) -> str:
    """RFC 5545 folding: no content line over 75 octets, never splitting a character.

    Octets are counted on the UTF-8 encoding; a continuation line spends one of its
    75 octets on the leading space, so its content budget is 74.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    cur, used, budget = "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if used + n > budget:
            parts.append(cur)
            cur, used, budget = "", 0, 74
        cur += ch
        used += n
    if cur:
        parts.append(cur)
    return "\r\n ".join(parts)


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def calendar_ics(card: DecisionCard, bill: Bill) -> str:
    """An all-day VEVENT on the card's deadline. Deterministic UID, CRLF line ends."""
    from fairbill.letters import _plain

    d = card.deadline
    kind = card.kind or ("clean" if card.clean else "review")
    summary = f"Fairbill: {card.bill_id} response deadline ({kind})"
    desc = f"{card.deadline_reason}. Evidence: {card.evidence_url}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fairbill//Decision deadline//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:{card.bill_id}-{d.isoformat()}-{kind}@fairbill.local",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(desc)}",
        f"LOCATION:{_esc(_plain(bill.hospital_name))}",
        "TRANSP:TRANSPARENT", "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


# ---- pipeline -------------------------------------------------------------

def gallery_truth() -> dict[str, dict]:
    if not TRUTH_PATH.exists():
        return {}
    doc = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    entries = doc if isinstance(doc, list) else doc.get("bills", doc.get("entries", []))
    return {e["id"]: e for e in entries}


def photo_path(bill_id: str) -> Path:
    entry = gallery_truth().get(bill_id) or {}
    rel = (entry.get("files") or {}).get("photo") or f"gallery/bills/{bill_id}_photo.jpg"
    return REPO_ROOT / rel


def _read_diffs(bill: Bill, bill_id: str) -> list[str]:
    """Where the photo read disagrees with truth.json. The demo shows what was read."""
    entry = gallery_truth().get(bill_id)
    if not entry:
        return []
    want = Bill(**entry["bill"])
    diffs = []
    wl = {ln.line_no: ln for ln in want.lines}
    gl = {ln.line_no: ln for ln in bill.lines}
    for no in sorted(set(wl) | set(gl)):
        a, b = wl.get(no), gl.get(no)
        if a is None or b is None:
            diffs.append(f"line {no} {'missing from the read' if b is None else 'read but not on the statement'}")
            continue
        if (a.code or "").upper() != (b.code or "").upper():
            diffs.append(f"line {no} code read as {b.code} (statement says {a.code})")
        if a.charge is not None and b.charge is not None and abs(a.charge - b.charge) > 0.005:
            diffs.append(f"line {no} charge read as {b.charge:,.2f} (statement says {a.charge:,.2f})")
    return diffs


_STATE: dict[str, dict] = {}


def _event(step: str, status: str, payload: Any, t0: float) -> dict:
    return {"step": step, "status": status, "payload": payload, "seconds": round(time.time() - t0, 2)}


# Median seconds per stage, measured on bill_02 with the shipped settings (three warm runs,
# 2026-09-14, _runs/2026-09-14_speed/exp/REPORT.md); the page shows these as "usually about
# N s". read/audit/card/letter are local Bedrock medians, which are compute-bound and match
# the Runtime. fetch is the download itself (median 8.4 s on the 39 MB file over three
# Runtime runs, TIMINGS.md baseline; 3.5 to 45 s seen); it starts with the run, so the
# fetch:start event carries what is left of it, not this number.
EXPECTED_S = {"read": 16.4, "fetch": 8.4, "audit": 13.7, "card": 0.0, "letter": 9.3}


def _start(step: str, payload: dict, t0: float) -> dict:
    """A start event, with the expected seconds the page counts against."""
    return _event(step, "start", {**payload, "expected_s": EXPECTED_S.get(step, 0.0)}, t0)


def _prefetch_hospital(bill_id: str) -> Optional[str]:
    """The hospital this gallery bill is expected to belong to, for the overlapped fetch only.

    Speculation, never evidence: the audit's hospital id always comes from resolving what the
    read actually returned, and a prefetch whose hospital does not match that is thrown away.
    """
    entry = gallery_truth().get(bill_id) or {}
    if entry.get("hospital_id"):
        return entry["hospital_id"]
    name = ((entry.get("bill") or {}).get("hospital_name") or "").strip()
    if not name:
        return None
    from fairbill.tools import resolve
    h = resolve(name)
    return h.get("hospital_id") if h.get("found") else None


async def run_pipeline(bill_id: str, session_id: str, today: Optional[date] = None,
                       live_fetch: bool = True) -> AsyncIterator[dict]:
    """read -> fetch -> audit -> card, one event per step, every step on the ledger."""
    from fairbill.audit import audit_async
    from fairbill.fetch import fetch_price_file
    from fairbill.reader import read_bill_with_meta
    from fairbill.schema import FetchReport
    from fairbill.tools import resolve

    today = today or date.today()
    t_all = time.time()

    # The price file is the same 39 MB download whoever asks for it, so it starts now and runs
    # while the model reads the photo. The read still decides which hospital the bill is from.
    prefetch_id = _prefetch_hospital(bill_id) if live_fetch else None
    prefetch_task = None
    # Cancelling the task does not stop the download thread, so the thread is asked to stop
    # too; without this the interpreter waits for the rest of the file at exit.
    prefetch_stop = threading.Event()
    # bytes so far from the download thread, read by the wait loop below when the download
    # outlives the read; a plain slot, written by one thread and read by one
    fetch_progress: dict = {"bytes": 0, "total": None}

    def _drop_prefetch(task):
        prefetch_stop.set()
        task.cancel()

    def _on_bytes(got: int, total):
        fetch_progress["bytes"], fetch_progress["total"] = got, total

    async def _timed_fetch(hospital_id: str):
        t = time.time()
        rep = await asyncio.to_thread(fetch_price_file, hospital_id, stop=prefetch_stop, progress=_on_bytes)
        return rep, round(time.time() - t, 2)  # the download's own seconds, not the wait for it

    if prefetch_id:
        prefetch_task = asyncio.create_task(_timed_fetch(prefetch_id))

    t0 = time.time()
    yield _start("read", {"bill_id": bill_id, "photo": photo_path(bill_id).name}, t0)
    try:
        bill, meta = await asyncio.to_thread(read_bill_with_meta, photo_path(bill_id))
    except Exception as exc:  # noqa: BLE001 - a failed read ends the run; never substitute truth.json
        if prefetch_task is not None:
            _drop_prefetch(prefetch_task)  # no read, no audit: the download has nothing to serve
        yield _event("read", "error", {"error": f"{type(exc).__name__}: {exc}"}, t0)
        return
    notes = list(meta.get("notes") or []) + _read_diffs(bill, bill_id)
    ledger.record(session_id, "read", f"read {photo_path(bill_id).name}", bill_id=bill_id,
                  evidence=[photo_path(bill_id).name])
    yield _event("read", "ok", {"bill": bill.model_dump(mode="json"), "notes": notes}, t0)

    t0 = time.time()
    hospital_hint, report, fetch_failed = None, None, False
    overlapped, fetch_seconds, waited_s = False, None, 0.0
    # resolving the hospital is deterministic and instant, so it happens before the start
    # event: the event can then say whether the download is already done, and how much of
    # its usual time is left when it is not
    try:
        h = resolve(f"{bill.hospital_name} {bill.hospital_address or ''}")
        hospital_hint = h.get("hospital_id")
    except Exception as exc:  # noqa: BLE001 - a resolver error is reported like a fetch error
        fetch_failed = True
        yield _start("fetch", {"live": live_fetch}, t0)
        yield _event("fetch", "error", {"error": f"{type(exc).__name__}: {exc}"}, t0)
    if prefetch_task is not None and hospital_hint != prefetch_id:
        _drop_prefetch(prefetch_task)  # the read named a different hospital; the guess is worthless
        prefetch_task = None
    if not fetch_failed:
        running_for = round(time.time() - t_all, 1)
        pending = prefetch_task is not None and not prefetch_task.done()
        start_payload = {"live": live_fetch, "running_for_s": running_for if prefetch_task is not None else 0.0,
                         "already_done": prefetch_task is not None and prefetch_task.done(),
                         "bytes": fetch_progress["bytes"], "total_bytes": fetch_progress["total"]}
        ev = _start("fetch", start_payload, t0)
        # expected_s is what is left: nothing when the download already landed, else the
        # usual download time minus how long it has been running (never under 1 s)
        ev["payload"]["expected_s"] = (max(1.0, round(EXPECTED_S["fetch"] - running_for, 1)) if pending
                                       else (0.0 if prefetch_task is not None else EXPECTED_S["fetch"]))
        yield ev
    try:
        if fetch_failed:
            pass
        elif live_fetch and hospital_hint and prefetch_task is not None:
            t_wait = time.time()
            last = -1
            while not prefetch_task.done():  # the download outlived the read: show it moving
                got = fetch_progress["bytes"]
                if got != last:
                    last = got
                    yield _event("fetch", "progress", {"bytes": got, "total_bytes": fetch_progress["total"]}, t0)
                await asyncio.sleep(0.5)
            waited_s = round(time.time() - t_wait, 2)
            try:
                report, fetch_seconds = await prefetch_task
                overlapped = waited_s < 0.5  # landed before the read did: nothing was waited for
            except Exception:  # noqa: BLE001 - a failed prefetch just means fetching now
                report = None
            if report is None:
                report = await asyncio.to_thread(fetch_price_file, hospital_hint, progress=_on_bytes)
        elif live_fetch and hospital_hint:
            report = await asyncio.to_thread(fetch_price_file, hospital_hint, progress=_on_bytes)
        elif hospital_hint:
            rec = hospital_record(hospital_hint) or {}
            report = FetchReport(hospital_id=hospital_hint, url=rec.get("mrf_url", ""), status="fixture",
                                 note="live fetch skipped; using the committed slice")
    except Exception as exc:  # noqa: BLE001 - a dead URL never stops the audit
        fetch_failed = True
        yield _event("fetch", "error", {"error": f"{type(exc).__name__}: {exc}"}, t0)
    if prefetch_task is not None and not prefetch_task.done():
        _drop_prefetch(prefetch_task)  # unused guess: never leave a 39 MB download running behind the run
    if report is not None:
        ledger.record(session_id, "fetch", f"{report.status} fetch of the hospital price file",
                      bill_id=bill_id, evidence=[report.url])
        payload = report.model_dump(mode="json")
        if fetch_seconds is not None:
            # The download started with the run. payload["seconds"] is untouched: FetchReport
            # already carries the stream's own seconds. fetch_seconds is the wall time of the
            # download (scheduling included), under a new key so it can never be confused
            # with either existing "seconds". overlapped means it finished before the read
            # did; otherwise waited_s is how long the page's fetch counter actually ran.
            payload["overlapped"] = overlapped
            payload["fetch_seconds"] = fetch_seconds
            payload["waited_s"] = waited_s
        yield _event("fetch", "ok", payload, t0)
    elif not fetch_failed:
        # every fetch:start gets a terminal event, even when there is nothing to fetch
        yield _event("fetch", "skipped", {"reason": "hospital not resolved"}, t0)

    t0 = time.time()
    yield _start("audit", {"hospital_id": hospital_hint}, t0)
    queue: asyncio.Queue = asyncio.Queue()
    timeline: list[dict] = []
    task = asyncio.create_task(audit_async(bill, bill_id, progress=queue, timeline=timeline))
    while True:
        getter = asyncio.ensure_future(queue.get())
        done, _pending = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield _event("audit", "progress", getter.result(), t0)
            continue
        getter.cancel()
        while not queue.empty():  # whatever landed while the audit was finishing
            yield _event("audit", "progress", queue.get_nowait(), t0)
        break
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001
        yield _event("audit", "error", {"error": f"{type(exc).__name__}: {exc}"}, t0)
        return
    ledger.record(session_id, "audit", f"{len(result.findings)} finding(s) against the hospital's file",
                  bill_id=bill_id,
                  evidence=[f"{ev.code} row {ev.source_line}" for f in result.findings for ev in f.evidence])
    yield _event("audit", "ok", result.model_dump(mode="json"), t0)

    t0 = time.time()
    yield _start("card", {"bill_id": bill_id, "status": result.status}, t0)
    card = build_card(bill, result, bill_id, today)
    _STATE[bill_id] = {"bill": bill, "result": result, "card": card, "today": today}
    ledger.record(session_id, "card", f"decision card, default {card.default_option}", bill_id=bill_id,
                  evidence=[card.evidence_url])
    yield _event("card", "ok", card.model_dump(mode="json"), t0)
    yield _event("done", "ok", {"bill_id": bill_id, "clean": card.clean,
                                "amount_at_stake": card.amount_at_stake}, t_all)


# ---- decide ---------------------------------------------------------------

def results_path(bill_id: str) -> Path:
    return RESULTS_DIR / f"{bill_id}.json"


def load_state(bill_id: str) -> Optional[dict]:
    """The bill, audit and card for a bill: from this process, else the precomputed cache.

    None when neither exists; the public accessor callers use instead of _STATE.
    """
    if bill_id in _STATE:
        return _STATE[bill_id]
    p = results_path(bill_id)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    st = {"bill": Bill(**doc["bill"]), "result": AuditResult(**doc["audit"]),
          "card": DecisionCard(**doc["card"]), "today": date.fromisoformat(doc["today"])}
    _STATE[bill_id] = st
    return st


async def decide_async(session_id: str, bill_id: str, option_id: str) -> dict:
    from fairbill.letters import draft_letter_async, render

    st = load_state(bill_id)
    if st is None:
        raise KeyError(f"no cached result for {bill_id}; run the pipeline first")
    bill, result, card = st["bill"], st["result"], st["card"]
    entry = ledger.record(session_id, "decision", f"patient chose {option_id}", bill_id=bill_id,
                          evidence=[card.evidence_url], undo="withdraw decision", actor="user")
    out: dict[str, Any] = {"letter": None, "ics": None, "ledger_entry": entry.model_dump(mode="json")}
    if option_id in ("dispute", "ask_provider", "itemized"):
        letter = await draft_letter_async(bill, result, card, option_id)
        out["letter"] = letter.model_dump(mode="json")
        out["letter_text"] = render(letter, st["today"])
        ledger.record(session_id, "letter", f"{letter.kind} letter drafted", bill_id=bill_id,
                      evidence=letter.citations, undo="discard draft")
    out["ics"] = calendar_ics(card, bill)
    ledger.record(session_id, "calendar", f"deadline {card.deadline.isoformat()} added",
                  bill_id=bill_id, evidence=[card.evidence_url], undo="remove entry")
    return out


def decide(session_id: str, bill_id: str, option_id: str) -> dict:
    """Sync wrapper. Inside a running event loop use decide_async."""
    return asyncio.run(decide_async(session_id, bill_id, option_id))


# ---- precompute -----------------------------------------------------------

async def precompute(bill_ids: Optional[list[str]] = None, today: Optional[date] = None,
                     live_fetch: bool = False) -> list[Path]:
    """Write gallery/results/<bill_id>.json: the pipeline output plus the default letter."""
    from fairbill.letters import draft_letter_async, render

    today = today or date.today()
    ids = bill_ids or sorted(gallery_truth())
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for bill_id in ids:
        session_id = f"precompute_{bill_id}"
        events = []
        async for ev in run_pipeline(bill_id, session_id, today=today, live_fetch=live_fetch):
            events.append(ev)
        st = _STATE.get(bill_id)
        if st is None:
            print(f"{bill_id}: pipeline did not finish; no result file written")
            continue
        card = st["card"]
        doc: dict[str, Any] = {
            "bill_id": bill_id, "today": today.isoformat(), "events": events,
            "bill": st["bill"].model_dump(mode="json"),
            "audit": st["result"].model_dump(mode="json"),
            "card": card.model_dump(mode="json"),
            "ics": calendar_ics(card, st["bill"]),
            "letter": None, "letter_text": None,
            "ledger": [e.model_dump(mode="json") for e in ledger.list(session_id)],
        }
        if not card.clean:
            try:
                letter = await draft_letter_async(st["bill"], st["result"], card, card.default_option)
            except Exception as exc:  # noqa: BLE001 - one letter must not lose the other bills
                print(f"{bill_id}: letter failed ({type(exc).__name__}: {exc}); no result file written")
                continue
            doc["letter"] = letter.model_dump(mode="json")
            doc["letter_text"] = render(letter, today)
        path = results_path(bill_id)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        written.append(path)
        print(f"wrote {path}")
    return written


# ---- CLI ------------------------------------------------------------------

def _print_card(card: DecisionCard) -> None:
    print("\nDECISION CARD")
    print(f"  {card.situation}")
    print(f"  Options ({len(card.options)}):")
    for o in card.options:
        mark = " <- default" if o.id == card.default_option else ""
        print(f"    [{o.id}] {o.label}: {o.consequence}{mark}")
    print(f"  Deadline: {card.deadline.isoformat()} ({card.deadline_reason})")
    print(f"  At stake: ${card.amount_at_stake:,.2f} ({card.basis} price)")
    print(f"  Evidence: {card.evidence_url}")
    print(f"  File rows: {', '.join(str(n) for n in card.evidence_lines) or '-'}")
    if card.needs_user_judgment:
        print("  Needs your judgment before sending.")


async def _run_bill(bill_id: str, live_fetch: bool, today: date, save_letters: bool) -> int:
    session_id = f"cli_{bill_id}"
    async for ev in run_pipeline(bill_id, session_id, today=today, live_fetch=live_fetch):
        head = f"[{ev['step']}:{ev['status']}] {ev['seconds']}s"
        if ev["step"] == "fetch" and ev["status"] == "ok":
            p = ev["payload"]
            print(f"{head} {p['status']} {p['bytes']:,} bytes, sha256 {(p['sha256'] or '-')[:12]}, "
                  f"matched_fixture={p['matched_fixture']} {p['note']}")
        elif ev["step"] == "audit" and ev["status"] == "ok":
            p = ev["payload"]
            print(f"{head} {len(p['findings'])} finding(s): "
                  f"{', '.join(f['kind'] + str(f['line_nos']) for f in p['findings']) or 'none'}")
        elif ev["step"] == "read" and ev["status"] == "ok":
            for n in ev["payload"]["notes"]:
                print(f"  read note: {n}")
            print(f"{head} {len(ev['payload']['bill']['lines'])} lines")
        else:
            print(head)
    st = _STATE.get(bill_id)
    if st is None:
        return 1
    card = st["card"]
    _print_card(card)
    if card.clean:
        print("\nNo letter: the bill matches the hospital's file.")
    else:
        out = await decide_async(session_id, bill_id, card.default_option)
        if out["letter"]:
            print("\nLETTER\n" + out["letter_text"])
            if save_letters:
                from fairbill.letters import Letter as _L, save
                p = save(_L(**out["letter"]), REPO_ROOT / "_runs" / f"{today.isoformat()}_letters")
                print(f"saved {p}")
    print("\nICS\n" + calendar_ics(card, st["bill"]).replace("\r\n", "\n"))
    print("LEDGER")
    for e in ledger.list(session_id):
        print(f"  {e.ts} {e.actor:5} {e.action:14} {e.why}"
              + (f" [undo: {e.undo}]" if e.undo else "") + (" [undone]" if e.undone else ""))
    return 0


async def _guard_demo(bill_id: str) -> int:
    from fairbill.guard import chat_async

    st = load_state(bill_id)
    if st is None:
        print(f"(no cached card for {bill_id}; running the guard demo without one)")
    card, bill = (st or {}).get("card"), (st or {}).get("bill")
    out = await chat_async(f"guard_demo_{bill_id}", bill_id, "just pay it", card, bill)
    print("patient: just pay it")
    print(f"reply: {out['reply']}")
    print("tool calls:")
    for c in out["tool_calls"]:
        print(f"  {c['name']}: {c['status'].upper()} {c['reason']}")
    denied = [c for c in out["tool_calls"] if c["status"] == "denied"]
    from fairbill import guard as _g
    print(f"pay_bill executions: {_g.pay_bill_calls} (must be 0)")
    return 0 if denied and _g.pay_bill_calls == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fairbill decision card, letter, calendar, ledger")
    ap.add_argument("--bill", help="gallery bill id, e.g. bill_02")
    ap.add_argument("--no-fetch", action="store_true", help="skip the live price-file fetch")
    ap.add_argument("--guard-demo", action="store_true", help='run the chat guard on "just pay it"')
    ap.add_argument("--precompute", action="store_true", help="write gallery/results/*.json for every bill")
    ap.add_argument("--save-letters", action="store_true", help="also write the letter to _runs/<date>_letters/")
    ap.add_argument("--today", default=None, help="ISO date to treat as today")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    if a.precompute:
        asyncio.run(precompute([a.bill] if a.bill else None, today=today, live_fetch=not a.no_fetch))
        return 0
    if a.guard_demo:
        return asyncio.run(_guard_demo(a.bill or "bill_02"))
    if a.bill:
        return asyncio.run(_run_bill(a.bill, not a.no_fetch, today, a.save_letters))
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
