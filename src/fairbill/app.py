"""Fairbill web tier: FastAPI + a phone-first single page (web/).

Three runtimes, same JSON:
  - in-process core (`fairbill.decisions`), the default;
  - AgentCore Runtime proxy when FAIRBILL_RUNTIME_ARN is set;
  - dev-only mock (FAIRBILL_MOCK=1) that replays A8-shaped events from gallery/truth.json.
Every mock response carries "mock": true.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
GALLERY = ROOT / "gallery"
DATA = ROOT / "data"
RUNS = ROOT / "_runs"

MOCK = os.environ.get("FAIRBILL_MOCK") == "1"
RUNTIME_ARN = os.environ.get("FAIRBILL_RUNTIME_ARN")

app = FastAPI(title="Fairbill", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


# ---------------------------------------------------------------- fixtures ---
def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def truth() -> list[dict]:
    return _json(GALLERY / "truth.json")


def truth_of(bill_id: str) -> dict:
    for row in truth():
        if row["id"] == bill_id:
            return row
    raise HTTPException(404, f"unknown bill {bill_id}")


def hospitals() -> dict[str, dict]:
    return {h["id"]: h for h in _json(DATA / "hospitals.json")["hospitals"]}


# ------------------------------------------------------------------- core ----
def core():
    """Import the Phase-5 core lazily so the app boots before it exists."""
    from fairbill import decisions, guard, ledger  # noqa: PLC0415

    return decisions, letters_mod(), ledger, guard


def letters_mod():
    from fairbill import letters  # noqa: PLC0415

    return letters


def core_ready() -> bool:
    try:
        core()
        return True
    except Exception:
        return False


async def _cached_events(bill_id: str) -> AsyncIterator[dict]:
    """Replay the precomputed run (gallery/results/<id>.json) for ?cached=1 or after a live error."""
    blob = _json(GALLERY / "results" / f"{bill_id}.json")
    for ev in blob["events"]:
        yield {**ev, "cached": True}


def has_cache(bill_id: str) -> bool:
    return (GALLERY / "results" / f"{bill_id}.json").is_file()


def session_of(header: str | None) -> str:
    return header or "anon-" + uuid.uuid4().hex[:8]


# ------------------------------------------------------------ mock runtime ---
_MOCK_LEDGER: dict[str, list[dict]] = {}
_MOCK_ICS: dict[str, str] = {}

_OPTIONS = {
    "dispute": ("Send the dispute letter",
                "Letter cites the hospital's own price file row; hospital has 30 days to answer"),
    "itemized": ("Request the itemized bill first",
                 "Delays payment; confirms each line before disputing"),
    "drop": ("Pay as billed", "You accept the charge"),
    "ask_provider": ("Ask the hospital to justify the visit level",
                     "Letter asks for the coding rationale against your visit summary"),
    "pay_in_network": ("Pay only the in-network share",
                       "Pay what the plan says you owe for in-network care; keep the letter on file"),
    "nsa": ("Send the No Surprises Act notice",
            "Cites 45 CFR 149.420; provider may bill only in-network cost sharing"),
}

_PLAN = {
    "duplicate": (["dispute", "itemized", "drop"], "dispute"),
    "above_cash": (["dispute", "itemized", "drop"], "dispute"),
    "unbundled": (["dispute", "itemized", "drop"], "itemized"),
    "upcoded": (["ask_provider", "itemized", "drop"], "ask_provider"),
    "out_of_network": (["nsa", "pay_in_network", "drop"], "nsa"),
}

FOOTER = ("Fairbill drafts letters from public price data. This is not legal advice. "
          "Patient names in this demo are fictional.")


def _opt(oid: str) -> dict:
    label, consequence = _OPTIONS[oid]
    return {"id": "dispute" if oid == "nsa" else oid, "label": label, "consequence": consequence}


def _mock_card(row: dict, today: date) -> dict:
    bill, planted = row["bill"], row["planted"]
    stmt = date.fromisoformat(bill["statement_date"]) if bill.get("statement_date") else today
    due = max(stmt + timedelta(days=30), today)  # D3: never a date that has already passed
    hosp = hospitals()[row["hospital_id"]]
    if not planted:
        return {
            "bill_id": row["id"], "status": "audited",
            "situation": ("This bill matches the hospital's published prices line by line. "
                          "Nothing to dispute."),
            "options": [{"id": "drop", "label": "Close the case",
                         "consequence": "Nothing is sent; the check is logged"}],
            "default_option": "drop",
            "deadline": due.isoformat(),
            "deadline_reason": "Payment due date printed on the statement",
            "evidence_url": hosp["mrf_url"], "evidence_lines": [],
            "amount_at_stake": 0.0, "basis": "unknown",
            "needs_user_judgment": False, "clean": True,
        }
    lead = max(planted, key=lambda f: f.get("amount_at_stake") or 0)
    ids, default = _PLAN[lead["kind"]]
    rest = [f["summary"] for f in planted if f is not lead]
    situation = lead["summary"] + ((" Also on this bill: " + " ".join(rest)) if rest else "")
    if lead["kind"] == "upcoded":
        situation += " This needs your judgment: does the visit summary match what happened?"
    oon = lead["kind"] == "out_of_network"
    return {
        "bill_id": row["id"], "status": "audited",
        "situation": situation,
        "options": [_opt(i) for i in ids],
        "default_option": "dispute" if default == "nsa" else default,
        "deadline": ((today + timedelta(days=120)) if oon else due).isoformat(),
        "deadline_reason": ("CMS patient-provider dispute window: 120 calendar days from the bill date"
                            if oon else
                            "Payment is due on this date; a written dispute before it keeps the "
                            "account out of collections"),
        "evidence_url": (lead["evidence"][0]["source_url"] if lead["evidence"] else hosp["mrf_url"]),
        "evidence_lines": [e["source_line"] for e in lead["evidence"]],
        "amount_at_stake": lead.get("amount_at_stake") or 0.0,
        "basis": lead.get("basis", "unknown"),
        "needs_user_judgment": bool(lead.get("needs_user_judgment")),
        "clean": False,
    }


def _mock_fetch(row: dict) -> dict:
    hosp = hospitals()[row["hospital_id"]]
    fx = ROOT / hosp["fixture"]
    blob = fx.read_bytes()
    return {
        "hospital_id": hosp["id"], "url": hosp["mrf_url"], "status": "fixture",
        "bytes": len(blob), "total_bytes": None, "seconds": 0.3,
        "sha256": hashlib.sha256(blob).hexdigest(), "matched_fixture": True,
        "note": "Mock mode: no live download. Lookups use the committed slice (fetched 2026-09-13).",
    }


def _mock_result(row: dict) -> dict:
    return {
        "bill_id": row["id"], "hospital_id": row["hospital_id"], "status": "audited",
        "findings": row["planted"], "clean": not row["planted"],
        "file_used": (row["planted"][0]["evidence"][0] if row["planted"] and row["planted"][0]["evidence"]
                      else None),
        "notes": row.get("notes", []),
    }


async def _mock_events(bill_id: str, session_id: str) -> AsyncIterator[dict]:
    row = truth_of(bill_id)
    steps = [
        ("read", row["bill"], 1.1),
        ("fetch", _mock_fetch(row), 0.4),
        ("audit", _mock_result(row), 1.4),
        ("card", _mock_card(row, date.today()), 0.2),
    ]
    for step, payload, secs in steps:
        yield {"step": step, "status": "start", "payload": None, "seconds": 0, "mock": True}
        await asyncio.sleep(0.35)
        _ledger_add(session_id, bill_id, step, f"{step} step completed (mock)", None)
        yield {"step": step, "status": "ok", "payload": payload, "seconds": secs, "mock": True}
    yield {"step": "done", "status": "ok", "payload": {"bill_id": bill_id}, "seconds": 3.1, "mock": True}


def _ledger_add(session_id: str, bill_id: str, action: str, why: str, undo: str | None) -> dict:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id, "bill_id": bill_id, "action": action, "why": why,
        "evidence": [], "undo": undo, "undone": False, "final_after": None, "actor": "agent",
    }
    _MOCK_LEDGER.setdefault(session_id, []).append(entry)
    return entry


def _mock_ics(card: dict, bill_id: str) -> str:
    day = card["deadline"].replace("-", "")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fairbill//demo//EN", "BEGIN:VEVENT",
        f"UID:fairbill-{bill_id}@fairbill.demo",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{day}",
        f"SUMMARY:Fairbill: {bill_id} response deadline",
        f"DESCRIPTION:{card['deadline_reason']} — {card['evidence_url']}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def _mock_letter(row: dict, card: dict, option_id: str) -> dict:
    bill, hosp = row["bill"], hospitals()[row["hospital_id"]]
    planted = row["planted"]
    lead = max(planted, key=lambda f: f.get("amount_at_stake") or 0) if planted else None
    ev = lead["evidence"][0] if lead and lead["evidence"] else None
    kind = {"itemized": "itemized_request"}.get(option_id, "dispute")
    if lead and lead["kind"] == "out_of_network" and option_id == "dispute":
        kind = "nsa_complaint"
    cites = ["45 CFR 180.50"]
    if ev:
        cites.append(f"{hosp['name']} standard charges file, line {ev['source_line']} "
                     f"(code {ev['code']}, cash price ${ev['discounted_cash']:.2f})")
    if lead and lead["kind"] == "out_of_network":
        cites.append("45 CFR 149.420")
    body = [
        f"Re: account {bill.get('account_number')}, statement dated {bill.get('statement_date')}",
        "",
        "To the billing department:",
        "",
        f"I am disputing one or more charges on this statement. {card['situation']}",
        "",
        f"Under 45 CFR 180.50 your hospital publishes a machine-readable file of standard charges. "
        f"I consulted that file at {card['evidence_url']} (fetched {ev['fetched_on'] if ev else '2026-09-13'}).",
    ]
    if ev:
        body += ["",
                 f"The file lists {ev['code']} — {ev['description']} at a gross charge of "
                 f"${ev['gross']:.2f} and a discounted cash price of ${ev['discounted_cash']:.2f} "
                 f"(line {ev['source_line']})."]
    body += ["", "Please correct the account and send a written response within 30 days.", "",
             "Sincerely,", bill["patient_name"]]
    return {
        "bill_id": row["id"], "kind": kind, "to_name": hosp["name"],
        "to_address": hosp.get("billing_address", ""), "from_name": bill["patient_name"],
        "re_line": f"Disputed charges, account {bill.get('account_number')}",
        "body": "\n".join(body), "citations": cites,
        "footer": FOOTER + " Mock draft: generated without a model call.",
    }


# ----------------------------------------------------------- runtime proxy ---
def _runtime_invoke(payload: dict, session_id: str) -> Any:
    import boto3  # noqa: PLC0415

    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=(session_id + "-" + "0" * 33)[:40],
        payload=json.dumps(payload).encode(),
    )
    body = resp["response"]
    raw = body.read() if hasattr(body, "read") else b"".join(body)
    return json.loads(raw or b"{}")


async def _runtime_events(bill_id: str, session_id: str, cached: bool) -> AsyncIterator[dict]:
    out = await asyncio.to_thread(
        _runtime_invoke, {"action": "run", "bill_id": bill_id, "cached": cached}, session_id)
    for ev in (out if isinstance(out, list) else [out]):
        yield ev


# -------------------------------------------------------------- endpoints ----
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"))


@app.get("/gallery/{name}")
def gallery_file(name: str) -> FileResponse:
    path = (GALLERY / "bills" / name).resolve()
    if not path.is_file() or GALLERY not in path.parents:
        raise HTTPException(404, "no such file")
    return FileResponse(path)


@app.get("/api/bills")
def api_bills() -> list[dict]:
    out = []
    for row in truth():
        bill = row["bill"]
        out.append({
            "id": row["id"], "hospital_name": bill["hospital_name"],
            "visit_type": bill.get("visit_type") or "Statement",
            "service_date": bill.get("service_date_start"),
            "photo_url": f"/gallery/{row['id']}_photo.jpg",
            "page_url": f"/gallery/{row['id']}_page.png",
            "planted_kind_hidden": False,
        })
    return out


@app.get("/api/bills/{bill_id}/truth-crops")
def api_crops(bill_id: str) -> dict:
    """Crop boxes only. Nothing from `planted` — the judge discovers the findings."""
    bill = truth_of(bill_id)["bill"]
    return {
        "bill_id": bill_id, "page_url": f"/gallery/{bill_id}_page.png",
        "fields": {k: v for k, v in (bill.get("crops") or {}).items()},
        "lines": [{"line_no": l["line_no"], "crop": l.get("crop")} for l in bill["lines"] if l.get("crop")],
    }


@app.post("/api/run/{bill_id}")
async def api_run(bill_id: str, request: Request,
                  x_fairbill_session: str | None = Header(None)) -> StreamingResponse:
    session_id = session_of(x_fairbill_session)
    cached = request.query_params.get("cached") == "1"
    truth_of(bill_id)  # 404 early

    async def gen() -> AsyncIterator[bytes]:
        try:
            if cached and has_cache(bill_id):
                src = _cached_events(bill_id)
            elif RUNTIME_ARN:
                src = _runtime_events(bill_id, session_id, cached)
            elif MOCK or not core_ready():
                src = _mock_events(bill_id, session_id)
            else:
                decisions = core()[0]
                src = decisions.run_pipeline(bill_id, session_id, live_fetch=not cached)
            async for ev in src:
                yield b"data: " + json.dumps(ev, default=str).encode() + b"\n\n"
        except Exception as exc:  # a dead step must not hang the page
            err = {"step": "done", "status": "error", "payload": {"error": str(exc)}, "seconds": 0}
            yield b"data: " + json.dumps(err).encode() + b"\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/decide/{bill_id}")
async def api_decide(bill_id: str, body: dict,
                     x_fairbill_session: str | None = Header(None)) -> JSONResponse:
    session_id = session_of(x_fairbill_session)
    option_id = (body or {}).get("option_id") or "dispute"
    if RUNTIME_ARN:
        out = await asyncio.to_thread(
            _runtime_invoke, {"action": "decide", "bill_id": bill_id, "option_id": option_id}, session_id)
        return JSONResponse(out)
    if not MOCK and core_ready():
        decisions = core()[0]
        out = await asyncio.to_thread(decisions.decide, session_id, bill_id, option_id)
        out = json.loads(json.dumps(out, default=lambda o: getattr(o, "model_dump", lambda: str(o))()))
        _MOCK_ICS[bill_id] = out.pop("ics", "") or _MOCK_ICS.get(bill_id, "")
        out["ics_url"] = f"/api/ics/{bill_id}"
        return JSONResponse(out)

    row = truth_of(bill_id)
    card = _mock_card(row, date.today())
    _MOCK_ICS[bill_id] = _mock_ics(card, bill_id)
    letter = None if option_id == "drop" else _mock_letter(row, card, option_id)
    entry = _ledger_add(session_id, bill_id, f"decision: {option_id}",
                        "You tapped this option on the Decision Card", "withdraw decision")
    if letter:
        _ledger_add(session_id, bill_id, "letter drafted", f"{letter['kind']} letter drafted",
                    "discard draft")
    _ledger_add(session_id, bill_id, "calendar entry", f"deadline {card['deadline']}", "remove entry")
    return JSONResponse({"letter": letter, "ics_url": f"/api/ics/{bill_id}",
                         "ledger_entry": entry, "mock": True})


@app.get("/api/ics/{bill_id}")
def api_ics(bill_id: str) -> Response:
    text = _MOCK_ICS.get(bill_id)
    if text is None:
        text = _mock_ics(_mock_card(truth_of(bill_id), date.today()), bill_id)
    return Response(text, media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{bill_id}.ics"'})


@app.get("/api/ledger")
async def api_ledger(x_fairbill_session: str | None = Header(None)) -> dict:
    session_id = session_of(x_fairbill_session)
    if RUNTIME_ARN:
        return await asyncio.to_thread(_runtime_invoke, {"action": "ledger"}, session_id)
    if not MOCK and core_ready():
        led = core()[2]
        rows = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in led.list(session_id)]
        return {"entries": rows}
    return {"entries": _MOCK_LEDGER.get(session_id, []), "mock": True}


@app.post("/api/undo/{entry_id}")
async def api_undo(entry_id: str, x_fairbill_session: str | None = Header(None)) -> JSONResponse:
    session_id = session_of(x_fairbill_session)
    if RUNTIME_ARN:
        return JSONResponse(await asyncio.to_thread(
            _runtime_invoke, {"action": "undo", "entry_id": entry_id}, session_id))
    if not MOCK and core_ready():
        led = core()[2]
        try:
            row = led.undo(session_id, entry_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:  # a final row, or one already undone
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(row.model_dump(mode="json") if hasattr(row, "model_dump") else row)
    for entry in _MOCK_LEDGER.get(session_id, []):
        if entry["id"] == entry_id:
            if not entry["undo"]:
                raise HTTPException(400, "this row cannot be undone")
            entry["undone"] = True
            return JSONResponse({**entry, "mock": True})
    raise HTTPException(404, "no such ledger entry")


@app.post("/api/chat/{bill_id}")
async def api_chat(bill_id: str, body: dict,
                   x_fairbill_session: str | None = Header(None)) -> JSONResponse:
    session_id = session_of(x_fairbill_session)
    text = (body or {}).get("text", "")
    if RUNTIME_ARN:
        return JSONResponse(await asyncio.to_thread(
            _runtime_invoke, {"action": "chat", "bill_id": bill_id, "text": text}, session_id))
    if not MOCK and core_ready():
        grd, decisions = core()[3], core()[0]
        state = decisions.load_state(bill_id) or {}
        card, bill = state.get("card"), state.get("bill")
        fn = getattr(grd, "chat_async", None)
        if fn:
            out = await fn(session_id, bill_id, text, card, bill)
        else:
            out = await asyncio.to_thread(grd.chat, session_id, bill_id, text, card, bill)
        if isinstance(out, str):
            out = {"reply": out, "tool_calls": []}
        return JSONResponse(json.loads(json.dumps(out, default=str)))
    await asyncio.sleep(0.4)
    _ledger_add(session_id, bill_id, "guard denial", "pay_bill blocked by the payment guard", None)
    return JSONResponse({
        "reply": ("I cannot move money. Fairbill never pays, and the payment tool was blocked "
                  "before it ran. What I can do is send the dispute letter or request the "
                  "itemized bill."),
        "tool_calls": [{"name": "pay_bill", "status": "denied",
                        "reason": "Guard rule: tool name matches pay|payment|charge_card|transfer. "
                                  "Fairbill never moves money."}],
        "mock": True,
    })


@app.get("/api/bench")
def api_bench() -> dict:
    audit = _json(RUNS / "2026-09-13_phase4_audit" / "bench.json")
    reader = _json(RUNS / "2026-09-13_phase3_reader" / "bench.json")
    return {
        "audit": {"planted": audit["totals"]["planted"], "matched": audit["totals"]["matched"],
                  "false_flags": audit["totals"]["false_flags"], "generated": audit["generated"],
                  "bills": [{"bill_id": b["bill_id"], "matched": b["matched"],
                             "false_flags": b["false_flags"], "seconds": b["seconds"]}
                            for b in audit["bills"]]},
        "reader": {"exact": reader["exact"], "total": reader["total"],
                   "model": reader["model_primary"], "generated": reader["generated"],
                   "seconds": round(sum(r["seconds"] for r in reader["rows"]) / len(reader["rows"]), 1)},
        "mode": "mock" if (MOCK or not core_ready()) else ("runtime" if RUNTIME_ARN else "live"),
    }


@app.get("/api/health")
def api_health() -> dict:
    return {"ok": True, "mock": MOCK or not core_ready(), "core_ready": core_ready(),
            "runtime": bool(RUNTIME_ARN), "ts": time.time()}
