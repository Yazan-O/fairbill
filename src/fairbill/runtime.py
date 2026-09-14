"""Bedrock AgentCore Runtime entrypoint.

One payload contract for every caller:
    {"action": "run"|"decide"|"chat"|"undo"|"ledger"|"bills"|"ics", bill_id?, option_id?,
     text?, entry_id?, cached?}
"run" streams the pipeline events; everything else returns JSON. The session id
comes from the Runtime request context, so the ledger is per session.

    python -m fairbill.runtime      # serves /invocations and /ping on port 8080
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from fairbill import ledger
from fairbill.decisions import (RESULTS_DIR, calendar_ics, decide_async, gallery_truth,
                                load_state, results_path, run_pipeline)

app = BedrockAgentCoreApp()

ACTIONS = ("run", "decide", "chat", "undo", "ledger", "bills", "ics")


def _session(context: Any) -> str:
    return getattr(context, "session_id", None) or "local"


def list_bills() -> list[dict]:
    """The gallery, without revealing what was planted."""
    out = []
    for bill_id, entry in sorted(gallery_truth().items()):
        b = entry["bill"]
        out.append({
            "id": bill_id,
            "hospital_name": b.get("hospital_name"),
            "visit_type": b.get("visit_type"),
            "service_date": b.get("service_date_start"),
            "photo_url": f"/gallery/{bill_id}_photo.jpg",
            "planted_kind_hidden": False,
        })
    return out


def cached_events(bill_id: str) -> list[dict] | None:
    p = results_path(bill_id)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    events = list(doc.get("events") or [])
    for ev in events:
        ev["cached"] = True
    return events


async def _stream(bill_id: str, session_id: str, cached: bool) -> AsyncIterator[dict]:
    if cached:
        events = cached_events(bill_id)
        if events is None:
            yield {"step": "done", "status": "error", "payload": {"error": f"no cached result for {bill_id}"},
                   "seconds": 0.0}
            return
        for ev in events:
            yield ev
        return
    try:
        async for ev in run_pipeline(bill_id, session_id, today=date.today(), live_fetch=True):
            yield ev
    except Exception as exc:  # noqa: BLE001 - a live failure is reported, never replayed from cache
        yield {"step": "done", "status": "error",
               "payload": {"error": f"live run failed ({type(exc).__name__}: {exc}); "
                                    "re-run with cached=true to replay the precomputed run"},
               "seconds": 0.0}


@app.entrypoint
async def invoke(payload, context):
    """Payload in, JSON or a stream of pipeline events out."""
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object with an 'action' field"}
    action = payload.get("action")
    if action not in ACTIONS:
        return {"error": f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}"}
    # D8: the Runtime session (set by the caller's InvokeAgentRuntime call) wins over any
    # session_id in the body, so one caller cannot read or undo another session's ledger.
    session_id = getattr(context, "session_id", None) or _session(context)
    bill_id = payload.get("bill_id")

    if action == "bills":
        return {"bills": list_bills()}
    if action == "ledger":
        return {"session_id": session_id,
                "entries": [e.model_dump(mode="json") for e in ledger.list(session_id)]}
    if action == "undo":
        entry_id = payload.get("entry_id")
        if not entry_id:
            return {"error": "undo needs entry_id"}
        try:
            return {"undone": ledger.undo(session_id, entry_id).model_dump(mode="json")}
        except (KeyError, ValueError) as exc:
            return {"error": str(exc)}
    if action == "ics":
        if not bill_id:
            return {"error": "ics needs bill_id"}
        st = load_state(bill_id)
        if not st:
            return {"error": f"no decision state for {bill_id}; run it first"}
        return {"ics": calendar_ics(st["card"], st["bill"])}
    if action == "decide":
        if not bill_id or not payload.get("option_id"):
            return {"error": "decide needs bill_id and option_id"}
        try:
            return await decide_async(session_id, bill_id, payload["option_id"])
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
    if action == "chat":
        from fairbill.guard import chat_async
        if not bill_id:
            return {"error": "chat needs bill_id"}
        st = load_state(bill_id) or {}
        return await chat_async(session_id, bill_id, payload.get("text") or "",
                                st.get("card"), st.get("bill"))

    if not bill_id:
        return {"error": "run needs bill_id"}
    return _stream(bill_id, session_id, bool(payload.get("cached")))


if __name__ == "__main__":
    app.run()
