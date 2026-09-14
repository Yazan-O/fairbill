"""Strands Evals cases for Fairbill, one per gallery bill, in two suites.

gallery/truth.json is the ground truth: the bill as printed plus the findings planted in it.
Suite "truth" feeds the audit the truth Bill directly; suite "e2e" starts from the photo and
runs reader.read_bill first, so the two rows for one bill separate audit errors from read errors.
"""
from __future__ import annotations

import json
from pathlib import Path

from strands_evals import Case

ROOT = Path(__file__).resolve().parents[1]
TRUTH_PATH = ROOT / "gallery" / "truth.json"
SUITES = ("truth", "e2e")


def load_truth() -> list[dict]:
    entries = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("bills") or entries.get("entries") or list(entries.values())
    return entries


_BY_NAME: dict[str, dict] = {}


def case_name(bill_id: str, suite: str) -> str:
    return f"{bill_id}_{suite}"


def truth_for(name: str | None) -> dict:
    """The truth entry behind a case name. Evaluators read it instead of carrying it in spans."""
    if not _BY_NAME:
        for entry in load_truth():
            for suite in SUITES:
                _BY_NAME[case_name(entry["id"], suite)] = entry
    return _BY_NAME[str(name)]


def photo_path(entry: dict) -> Path:
    rel = (entry.get("files") or {}).get("photo") or f"gallery/bills/{entry['id']}_photo.jpg"
    return ROOT / rel


def build_cases(suite: str) -> list[Case]:
    """One Case per bill. expected_output is the planted (kind, line_nos) list the audit must find."""
    out = []
    for entry in load_truth():
        planted = [{"kind": f["kind"], "line_nos": sorted(f["line_nos"])} for f in entry.get("planted", [])]
        out.append(Case(
            name=case_name(entry["id"], suite),
            input=entry["id"],
            expected_output={"planted": planted},
            metadata={"suite": suite, "hospital_id": entry.get("hospital_id"),
                      "clean": not planted, "photo": str(photo_path(entry).relative_to(ROOT))},
        ))
    return out
