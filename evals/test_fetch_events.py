"""Kills three claims behind the phone finding "fetch showed no expected time, took 44 s,
then said downloaded": (a) fetch:start carries expected_s and the download's age,
(b) a download that outlives the read streams progress bytes and lands as waited, not
overlapped, (c) a download that finished under the read lands as overlapped with 0 wait.
The models are stubbed: the read returns bill_02 from truth.json after a set delay and the
download is a fake that ticks bytes for a set time. No Bedrock, no network.
Run: PYTHONPATH=src python -m pytest evals/test_fetch_events.py -q
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from fairbill import decisions
from fairbill.schema import Bill, FetchReport

ROOT = Path(__file__).resolve().parents[1]


def _bill_02() -> Bill:
    item = next(x for x in json.loads((ROOT / "gallery/truth.json").read_text()) if x["id"] == "bill_02")
    return Bill.model_validate(item["bill"])


def _stub(monkeypatch, read_s: float, download_s: float):
    import fairbill.fetch
    import fairbill.reader

    def fake_read(_path):
        time.sleep(read_s)
        return _bill_02(), {"notes": []}

    def fake_fetch(hospital_id, timeout_s=0, max_bytes=None, stop=None, progress=None):
        total, got, t0 = 1000, 0, time.time()
        while time.time() - t0 < download_s:
            time.sleep(0.1)
            got = min(total, int(total * (time.time() - t0) / download_s))
            if progress:
                progress(got, total)
        return FetchReport(hospital_id=hospital_id, url="https://example.test/file.csv", status="live",
                           bytes=total, total_bytes=total, seconds=download_s)

    monkeypatch.setattr(fairbill.reader, "read_bill_with_meta", fake_read)
    monkeypatch.setattr(fairbill.fetch, "fetch_price_file", fake_fetch)
    monkeypatch.setattr(decisions, "_read_diffs", lambda bill, bill_id: [])


async def _fetch_events(bill_id="bill_02"):
    out = []
    async for ev in decisions.run_pipeline(bill_id, "test-session", live_fetch=True):
        if ev["step"] == "fetch":
            out.append(ev)
        if ev["step"] == "fetch" and ev["status"] in ("ok", "error", "skipped"):
            break
    return out


@pytest.fixture(autouse=True)
def _quiet_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(decisions.ledger, "record", lambda *a, **k: None)


def test_download_that_outlives_the_read_streams_progress_and_lands_as_waited(monkeypatch):
    _stub(monkeypatch, read_s=0.3, download_s=2.0)
    evs = asyncio.run(_fetch_events())
    start, ok = evs[0], evs[-1]
    assert start["status"] == "start" and ok["status"] == "ok"
    assert start["payload"]["already_done"] is False
    assert start["payload"]["expected_s"] >= 1.0  # what is left, never blank
    assert start["payload"]["running_for_s"] >= 0.3
    progress = [e for e in evs if e["status"] == "progress"]
    assert progress and progress[-1]["payload"]["bytes"] > progress[0]["payload"]["bytes"]
    assert ok["payload"]["overlapped"] is False
    assert ok["payload"]["waited_s"] >= 1.0
    assert ok["payload"]["fetch_seconds"] >= 2.0


def test_download_that_finished_under_the_read_lands_as_overlapped(monkeypatch):
    _stub(monkeypatch, read_s=1.0, download_s=0.3)
    evs = asyncio.run(_fetch_events())
    start, ok = evs[0], evs[-1]
    assert start["payload"]["already_done"] is True
    assert start["payload"]["expected_s"] == 0.0
    assert not [e for e in evs if e["status"] == "progress"]
    assert ok["payload"]["overlapped"] is True
    assert ok["payload"]["waited_s"] < 0.5
