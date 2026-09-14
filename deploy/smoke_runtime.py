"""Exercise the deployed AgentCore Runtime through the data plane.

Every action the page uses, against the real runtime, with the live 39 MB
Norman Regional price-file download read event by event as it arrives.
Transcripts land in _runs/2026-09-13_phase6_deploy/smoke_*.txt.

    python deploy/smoke_runtime.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = REPO / "_runs" / "2026-09-13_phase6_deploy"
DEPLOY_JSON = RUN_DIR / "DEPLOY.json"
REGION = "us-east-1"

sys.path.insert(0, str(REPO / "src"))
from fairbill.config import load_env  # noqa: E402

load_env()

# A runtimeSessionId must be at least 33 characters ("Generate a unique session ID
# for each user or conversation with at least 33 characters", runtime-sessions.md).
SESSION = f"fairbill-smoke-{uuid.uuid4().hex}"[:40]

# A pipeline step can run for minutes with no bytes on the wire, so the default
# 60s read timeout would kill the stream mid-run.
client = boto3.client("bedrock-agentcore", region_name=REGION,
                      config=Config(read_timeout=900, connect_timeout=20,
                                    retries={"max_attempts": 0}))
ARN = json.loads(DEPLOY_JSON.read_text(encoding="utf-8"))["runtime_arn"]
log_lines: list[str] = []


def log(text: str = "") -> None:
    print(text)
    log_lines.append(text)


def invoke(payload: dict, session: str = SESSION):
    return client.invoke_agent_runtime(
        agentRuntimeArn=ARN, runtimeSessionId=session, contentType="application/json",
        accept="application/json", payload=json.dumps(payload).encode())


def call_json(payload: dict, session: str = SESSION) -> dict:
    r = invoke(payload, session)
    body = r["response"].read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"error": "non-JSON", "raw": body[:1000]}


def call_stream(payload: dict, session: str = SESSION) -> list[dict]:
    """Read the SSE frames as they arrive; each one is printed with the wall
    clock at arrival so a buffered response would be visible as a single burst."""
    r = invoke(payload, session)
    log(f"  contentType: {r.get('contentType')}")
    events: list[dict] = []
    t0 = time.time()
    buf = b""
    for chunk in r["response"].iter_chunks(chunk_size=1):
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev)
            log(f"  +{time.time() - t0:6.1f}s  {ev.get('step'):>6} {ev.get('status'):<6} "
                f"{ev.get('seconds')}s")
            payload_ = ev.get("payload") or {}
            if ev.get("step") == "fetch" and ev.get("status") == "ok":
                log("           fetch payload: " + json.dumps(payload_, default=str))
    return events


def write(name: str) -> None:
    (RUN_DIR / name).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    log_lines.clear()
    print(f"-> {RUN_DIR / name}")


def main() -> int:
    failures: list[str] = []
    log(f"runtime: {ARN}")
    log(f"session: {SESSION} ({len(SESSION)} chars)")

    log("\n== bills ==")
    out = call_json({"action": "bills"})
    log(json.dumps(out, indent=1)[:1500])
    if len(out.get("bills") or []) < 6:
        failures.append("bills returned fewer than 6 gallery items")
    write("smoke_bills.txt")

    log(f"runtime: {ARN}\n\n== run bill_06 (live) ==")
    ev6 = call_stream({"action": "run", "bill_id": "bill_06", "cached": False})
    if not any(e.get("step") == "done" and e.get("status") == "ok" for e in ev6):
        failures.append("bill_06 run did not finish ok")
    write("smoke_run_bill_06.txt")

    log(f"runtime: {ARN}\n\n== run bill_02 (live) ==")
    ev2 = call_stream({"action": "run", "bill_id": "bill_02", "cached": False})
    fetch = next((e for e in ev2 if e.get("step") == "fetch" and e.get("status") == "ok"), None)
    fp = (fetch or {}).get("payload") or {}
    log("\nfetch check:")
    log(f"  status         {fp.get('status')}          (want live)")
    log(f"  bytes          {fp.get('bytes')}           (want 39141747)")
    log(f"  sha256         {fp.get('sha256')}")
    log(f"  matched_fixture {fp.get('matched_fixture')} (want True)")
    if fp.get("bytes") != 39141747:
        failures.append(f"bill_02 live fetch got {fp.get('bytes')} bytes, want 39141747")
    if not str(fp.get("sha256", "")).startswith("4b79577f"):
        failures.append(f"bill_02 sha256 is {fp.get('sha256')}, want 4b79577f...")
    if fp.get("matched_fixture") is not True:
        failures.append(f"bill_02 matched_fixture is {fp.get('matched_fixture')}, want True")
    write("smoke_run_bill_02.txt")

    log(f"runtime: {ARN}\n\n== decide bill_02 dispute ==")
    dec = call_json({"action": "decide", "bill_id": "bill_02", "option_id": "dispute"})
    log(json.dumps({k: (v if k != "letter_text" else str(v)[:400]) for k, v in dec.items()},
                   indent=1, default=str)[:3000])
    entry_id = ((dec.get("ledger_entry") or {}).get("id"))
    if not dec.get("letter"):
        failures.append("decide returned no letter")

    log("\n== ics bill_02 ==")
    ics = call_json({"action": "ics", "bill_id": "bill_02"})
    log((ics.get("ics") or ics.get("error") or "")[:800])
    if not (ics.get("ics") or "").startswith("BEGIN:VCALENDAR"):
        failures.append("ics action did not return a calendar")

    log("\n== chat bill_02 'just pay it' ==")
    chat = call_json({"action": "chat", "bill_id": "bill_02", "text": "just pay it"})
    log(json.dumps(chat, indent=1, default=str)[:2500])
    if not any(t.get("status") == "denied" for t in (chat.get("tool_calls") or [])):
        failures.append("chat did not show a denied pay_bill tool call")

    log("\n== ledger ==")
    led = call_json({"action": "ledger"})
    log(json.dumps(led, indent=1, default=str)[:3000])

    log(f"\n== undo {entry_id} ==")
    undo = call_json({"action": "undo", "entry_id": entry_id}) if entry_id else {"error": "no entry"}
    log(json.dumps(undo, indent=1, default=str)[:1200])
    after = call_json({"action": "ledger"})
    row = next((e for e in after.get("entries", []) if e.get("id") == entry_id), None)
    log(f"\nafter undo: entry {entry_id} undone={row and row.get('undone')}")
    if not (row and row.get("undone")):
        failures.append("undo did not mark the decision row undone")
    write("smoke_actions.txt")

    print("\n" + ("FAILURES:\n  " + "\n  ".join(failures) if failures else "all runtime checks passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
