"""End-to-end check of the public Function URL: the page, its assets, the API,
and a streamed run read frame by frame as the browser would read it.

    python deploy/smoke_proxy.py [--bill bill_02] [--cached]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = REPO / "_runs" / "2026-09-13_phase6_deploy"
DEPLOY_JSON = RUN_DIR / "DEPLOY.json"
SESSION = "smoke-proxy-" + time.strftime("%H%M%S")

lines: list[str] = []
results: list[tuple[str, bool]] = []


def log(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok))
    log(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# timeout is urllib's per-socket-read budget. A live bill_02 read step is ~450s
# of dead air between SSE frames, so it has to match the Lambda ceiling.
def request(url: str, method: str = "GET", body: dict | None = None, timeout: int = 900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "X-Fairbill-Session": SESSION,
        "Accept": "*/*", "User-Agent": "fairbill-smoke/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bill", default="bill_02")
    ap.add_argument("--cached", action="store_true")
    a = ap.parse_args()

    base = json.loads(DEPLOY_JSON.read_text(encoding="utf-8"))["url"].rstrip("/")
    log(f"url: {base}")
    log(f"session: {SESSION}")

    r = request(base + "/")
    html = r.read().decode("utf-8", "replace")
    check("GET /", r.status == 200 and "<html" in html.lower(),
          f"{r.status} {r.headers.get('Content-Type')} {len(html)} bytes")

    for path in ("/static/style.css", "/static/app.js"):
        r = request(base + path)
        blob = r.read()
        check(f"GET {path}", r.status == 200 and len(blob) > 500,
              f"{r.headers.get('Content-Type')} {len(blob)} bytes")

    r = request(f"{base}/gallery/{a.bill}_photo.jpg")
    blob = r.read()
    check(f"GET /gallery/{a.bill}_photo.jpg", r.status == 200 and blob[:2] == b"\xff\xd8",
          f"{r.headers.get('Content-Type')} {len(blob)} bytes")

    bills = json.loads(request(base + "/api/bills").read())
    check("GET /api/bills", len(bills) >= 6, f"{len(bills)} bills")

    crops = json.loads(request(f"{base}/api/bills/{a.bill}/truth-crops").read())
    check(f"GET /api/bills/{a.bill}/truth-crops", crops.get("bill_id") == a.bill,
          f"{len(crops.get('lines') or [])} line crops")

    bench = json.loads(request(base + "/api/bench").read())
    check("GET /api/bench", bench.get("mode") == "runtime",
          f"mode={bench.get('mode')} reader {bench['reader']['exact']}/{bench['reader']['total']} "
          f"audit {bench['audit']['matched']}/{bench['audit']['planted']}")

    health = json.loads(request(base + "/api/health").read())
    check("GET /api/health", health.get("ok") and health.get("mode") == "runtime", json.dumps(health))

    log(f"\n== POST /api/run/{a.bill} (streamed) ==")
    url = f"{base}/api/run/{a.bill}" + ("?cached=1" if a.cached else "")
    r = request(url, method="POST", body={})
    log(f"  content-type: {r.headers.get('Content-Type')}")
    t0 = time.time()
    buf, events, arrivals = b"", [], []
    while True:
        chunk = r.read(1)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev)
            arrivals.append(round(time.time() - t0, 1))
            log(f"  +{arrivals[-1]:6.1f}s  {ev.get('step'):>6} {ev.get('status'):<6} {ev.get('seconds')}s")
            if ev.get("step") == "fetch" and ev.get("status") == "ok":
                log("           " + json.dumps(ev.get("payload") or {}, default=str))
    spread = len(set(arrivals)) > 1
    check(f"POST /api/run/{a.bill}",
          r.headers.get("Content-Type", "").startswith("text/event-stream") and len(events) >= 5,
          f"{len(events)} events over {arrivals[-1] if arrivals else 0}s")
    # A buffered Function URL would deliver every frame at the same instant.
    check("run is progressive, not buffered", spread, f"arrival times {arrivals}")

    log("\n== decide / ics / chat / ledger ==")
    dec = json.loads(request(f"{base}/api/decide/{a.bill}", method="POST",
                             body={"option_id": "dispute"}).read())
    check(f"POST /api/decide/{a.bill}", dec.get("ics_url") == f"/api/ics/{a.bill}" and dec.get("letter"),
          f"keys={','.join(dec)}")

    r = request(f"{base}/api/ics/{a.bill}")
    ics = r.read().decode()
    check(f"GET /api/ics/{a.bill}", ics.startswith("BEGIN:VCALENDAR"),
          f"{r.headers.get('Content-Type')} {r.headers.get('Content-Disposition')} {len(ics)} bytes")

    chat = json.loads(request(f"{base}/api/chat/{a.bill}", method="POST",
                              body={"text": "just pay it"}).read())
    denied = [t for t in (chat.get("tool_calls") or []) if t.get("status") == "denied"]
    check(f"POST /api/chat/{a.bill}", bool(chat.get("reply")) and bool(denied),
          f"denied={[t.get('name') for t in denied]}")
    log("  reply: " + str(chat.get("reply"))[:300])

    led = json.loads(request(base + "/api/ledger").read())
    check("GET /api/ledger", isinstance(led.get("entries"), list), f"{len(led.get('entries') or [])} entries")

    bad = [n for n, ok in results if not ok]
    log(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    if bad:
        log("FAILED: " + ", ".join(bad))
    (RUN_DIR / "smoke_proxy.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {RUN_DIR / 'smoke_proxy.txt'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
