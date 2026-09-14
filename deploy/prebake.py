"""Capture the read-only API responses from the real FastAPI app into JSON the
Lambda proxy serves as files. Run without FAIRBILL_MOCK so the numbers are the
live ones; FAIRBILL_RUNTIME_ARN is set so `mode`/`runtime` describe the
deployed shape, which is what the proxy will actually be.

    python deploy/prebake.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "deploy" / "lambda_proxy" / "prebaked"

os.environ.pop("FAIRBILL_MOCK", None)
os.environ["FAIRBILL_RUNTIME_ARN"] = os.environ.get("FAIRBILL_RUNTIME_ARN", "pending")
sys.path.insert(0, str(REPO / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from fairbill.app import app  # noqa: E402

OUT.mkdir(parents=True, exist_ok=True)
c = TestClient(app)


def grab(path: str, name: str) -> None:
    r = c.get(path)
    r.raise_for_status()
    (OUT / name).write_text(json.dumps(r.json(), indent=1), encoding="utf-8")
    print(f"{path:45s} -> {name} ({len(r.content):,} bytes)")


grab("/api/bills", "bills.json")
grab("/api/bench", "bench.json")
grab("/api/health", "health.json")
crops = {}
for row in c.get("/api/bills").json():
    r = c.get(f"/api/bills/{row['id']}/truth-crops")
    r.raise_for_status()
    crops[row["id"]] = r.json()
(OUT / "truth_crops.json").write_text(json.dumps(crops, indent=1), encoding="utf-8")
print(f"{'/api/bills/<id>/truth-crops':45s} -> truth_crops.json ({len(crops)} bills)")
print("bench mode:", json.loads((OUT / "bench.json").read_text())["mode"])
print("health:", (OUT / "health.json").read_text().replace("\n", " "))
