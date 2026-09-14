"""Live fetch of a hospital's own published standard-charges file.

Fairbill's lookups always run against the committed fixture slice; this module
exists so the demo can show that the slice comes from a file the hospital
publishes right now, at the URL printed in the report. Never blocks the
pipeline longer than timeout_s, never writes into data/.
"""
from __future__ import annotations

import hashlib
import os
import threading  # noqa: F401 - the stop-event type in fetch_price_file
from typing import Callable  # noqa: F401 - the progress-callback type
import time
import urllib.request
from pathlib import Path

from fairbill.mrf import MRF
from fairbill.schema import FetchReport
from fairbill.tools import fixture_for, gallery_codes, hospital_record

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "_runs" / "fetch_cache"
CHUNK = 1 << 20

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

# OU publishes a 1.5 GB zip; take a sample to prove the URL is live, no more.
PARTIAL_MB = 8
PARTIAL_HOSPITALS = {"ou_health_oumc"}
PARTIAL_NOTE = "lookups use the committed slice (fetched 2026-09-13)"


def cache_dir() -> Path:
    return Path(os.environ.get("FAIRBILL_CACHE_DIR") or DEFAULT_CACHE_DIR)


def _cache_path(hospital_id: str, url: str) -> Path:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    ext = ".zip" if url.rstrip("/").endswith("mrf") or url.endswith(".zip") else ".csv"
    return d / f"{hospital_id}{ext}"


def _verify_against_fixture(hospital_id: str, path: Path) -> bool | None:
    """True when the downloaded file gives the same gross/cash as the fixture for
    every gallery code it carries. None when the file cannot be parsed."""
    fx = fixture_for(hospital_id)
    if fx is None:
        return None
    want: dict[str, set[tuple]] = {}
    for it in fx.doc["items"]:
        for c in it["codes"]:
            want.setdefault(c["code"].upper(), set()).add((it.get("gross"), it.get("discounted_cash")))
    codes = {c for c in gallery_codes() if c in want}
    if not codes:
        return None
    try:
        mrf = MRF(path)
        seen: dict[str, set[tuple]] = {}
        for it in mrf.items():
            for c, _t in it.codes:
                cu = c.strip().upper()
                if cu in codes:
                    seen.setdefault(cu, set()).add((it.gross, it.discounted_cash))
    except Exception:  # noqa: BLE001 - a partial or re-published file is not a crash
        return None
    if not seen:
        return None
    return all(seen[c] <= want[c] for c in seen)


# From us-east-1 the 39 MB Norman Regional file needs about 32s, so the local
# 20s budget would always report live_partial once deployed.
DEFAULT_TIMEOUT_S = float(os.environ.get("FAIRBILL_FETCH_TIMEOUT") or 20)


def fetch_price_file(hospital_id: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                     max_bytes: int | None = None,
                     stop: "threading.Event | None" = None,
                     progress: "Callable[[int, int | None], None] | None" = None) -> FetchReport:
    """Stream the hospital's price-file URL; report bytes, seconds, sha256.

    status "live" when the whole file arrived, "live_partial" when a byte cap or
    the timeout cut it short, "fixture" when nothing arrived.

    stop is the cooperative cancel for the overlapped prefetch in decisions.run_pipeline:
    cancelling the asyncio task does not stop this thread, and the download would then hold
    the interpreter at exit (measured 4.8s on the 39 MB file, exp/cancel_probe.py). Setting
    the event makes the chunk loop return at the next 1 MB boundary.

    progress(bytes_so_far, total_bytes_or_None) is called after every chunk, so the page
    can show a moving bar when the download outlives the read it started under.
    """
    rec = hospital_record(hospital_id)
    if rec is None:
        return FetchReport(hospital_id=hospital_id, url="", status="fixture",
                           note=f"unknown hospital id {hospital_id}; using the committed slice")
    url = rec["mrf_url"]
    if max_bytes is None and hospital_id in PARTIAL_HOSPITALS:
        max_bytes = PARTIAL_MB << 20

    out = _cache_path(hospital_id, url)
    h = hashlib.sha256()
    got, total, partial, note = 0, None, False, ""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp, out.open("wb") as fh:
            cl = resp.headers.get("Content-Length")
            total = int(cl) if cl and cl.isdigit() else None
            while True:
                if time.monotonic() - t0 > timeout_s:
                    partial, note = True, f"stopped at the {timeout_s:g}s budget"
                    break
                if stop is not None and stop.is_set():
                    partial, note = True, "cancelled: the read named a different hospital"
                    break
                if max_bytes is not None and got >= max_bytes:
                    partial, note = True, f"stopped at the {max_bytes >> 20} MB sample"
                    break
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress is not None:
                    progress(got, total)
    except Exception as exc:  # noqa: BLE001 - the demo must survive a dead URL
        return FetchReport(hospital_id=hospital_id, url=url, status="fixture", bytes=got,
                           total_bytes=total, seconds=round(time.monotonic() - t0, 2),
                           note=f"{type(exc).__name__}: {exc}; using the committed slice")
    seconds = round(time.monotonic() - t0, 2)
    if got == 0:
        return FetchReport(hospital_id=hospital_id, url=url, status="fixture", seconds=seconds,
                           total_bytes=total, note="no bytes returned; using the committed slice")
    complete = not partial and (total is None or got >= total)
    status = "live" if complete else "live_partial"
    matched = _verify_against_fixture(hospital_id, out) if complete else None
    if status == "live_partial" and hospital_id in PARTIAL_HOSPITALS:
        note = f"{note}; {PARTIAL_NOTE}" if note else PARTIAL_NOTE
    if matched is False:
        note = (note + "; " if note else "") + "live file disagrees with the committed slice on a gallery code"
    return FetchReport(hospital_id=hospital_id, url=url, status=status, bytes=got, total_bytes=total,
                       seconds=seconds, sha256=h.hexdigest(), matched_fixture=matched, note=note)


if __name__ == "__main__":  # pragma: no cover - manual proof run
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("hospital_id")
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--max-mb", type=int, default=None)
    a = ap.parse_args()
    r = fetch_price_file(a.hospital_id, timeout_s=a.timeout,
                         max_bytes=(a.max_mb << 20) if a.max_mb else None)
    print(r.model_dump_json(indent=2))
