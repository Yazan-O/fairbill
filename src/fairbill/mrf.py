"""Hospital price-file (MRF) parser written against the CMS Hospital Price
Transparency CSV data dictionary v3.0 (data/cms/CSV_README.md).

Handles both CSV layouts the dictionary allows:
  tall: one row per (item, payer, plan); payer_name / plan_name columns.
  wide: one row per item; payer-specific columns named
        standard_charge|<payer>|<plan>|negotiated_dollar etc.

Rows 1-2 are the general data elements, row 3 is the item header, row 4+ are
items.  Every item is normalised into one `Item` dict so the rest of Fairbill
never sees the layout difference.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RAW_DIR = DATA_DIR / "raw"
FIXTURE_DIR = DATA_DIR / "fixtures"

PAYER_FIELDS = {
    "negotiated_dollar", "negotiated_percentage", "negotiated_algorithm",
    "methodology", "median_amount", "10th_percentile", "90th_percentile",
    "count", "additional_payer_notes",
}


def _norm(h: str) -> str:
    """Header normalisation per dictionary: case-insensitive, spaces around pipes ignored."""
    h = h.lstrip("﻿").strip().lower()
    return "|".join(p.strip() for p in h.split("|"))


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


@dataclass
class Header:
    hospital_name: str
    last_updated_on: str
    version: str
    location_names: list[str]
    addresses: list[str]
    npis: list[str]
    license: dict[str, str]
    attester_name: str
    attestation: bool
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class PayerCharge:
    payer: str
    plan: str
    negotiated_dollar: float | None = None
    negotiated_percentage: float | None = None
    negotiated_algorithm: str | None = None
    methodology: str | None = None
    median_amount: float | None = None
    p10: float | None = None
    p90: float | None = None
    count: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Item:
    description: str
    codes: list[tuple[str, str]]          # [(code, code_type)]
    setting: str
    billing_class: str | None
    modifiers: str | None
    gross: float | None
    discounted_cash: float | None
    min_negotiated: float | None
    max_negotiated: float | None
    notes: str | None
    payers: list[PayerCharge]
    source_line: int                      # 1-based line number in the raw CSV

    def to_dict(self) -> dict:
        d = {
            "description": self.description,
            "codes": [{"code": c, "type": t} for c, t in self.codes],
            "setting": self.setting,
            "billing_class": self.billing_class,
            "modifiers": self.modifiers,
            "gross": self.gross,
            "discounted_cash": self.discounted_cash,
            "min_negotiated": self.min_negotiated,
            "max_negotiated": self.max_negotiated,
            "notes": self.notes,
            "source_line": self.source_line,
            "payers": [p.to_dict() for p in self.payers],
        }
        return {k: v for k, v in d.items() if v not in (None, "", [])}

    def has_code(self, code: str) -> bool:
        code = code.strip().upper()
        return any(c.strip().upper() == code for c, _ in self.codes)


class MRF:
    """Streaming reader over a CMS v3 CSV price file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.header: Header | None = None
        self.layout: str | None = None   # "tall" | "wide"
        self.columns: list[str] = []

    # ---- header ---------------------------------------------------------
    def _open(self):
        return open(self.path, "r", encoding="utf-8-sig", newline="")

    def read_header(self) -> Header:
        with self._open() as f:
            r = csv.reader(f)
            h1 = [_norm(c) for c in next(r)]
            v1 = next(r)
            h3 = [_norm(c) for c in next(r)]
        raw = {h: v.strip() for h, v in zip(h1, v1) if h}
        lic = {h.split("|", 1)[1].upper(): v for h, v in raw.items() if h.startswith("license_number|")}
        attest = next((v for h, v in raw.items() if h.startswith("to the best of its knowledge")), "")
        self.header = Header(
            hospital_name=raw.get("hospital_name", ""),
            last_updated_on=raw.get("last_updated_on", ""),
            version=raw.get("version", ""),
            location_names=[s.strip() for s in raw.get("location_name", "").split("|") if s.strip()],
            addresses=[s.strip() for s in raw.get("hospital_address", "").split("|") if s.strip()],
            npis=[s.strip() for s in raw.get("type_2_npi", "").split("|") if s.strip()],
            license=lic,
            attester_name=raw.get("attester_name", ""),
            attestation=attest.lower() == "true",
            raw=raw,
        )
        self.columns = h3
        self.layout = "tall" if "payer_name" in h3 else "wide"
        return self.header

    # ---- rows -----------------------------------------------------------
    def _code_pairs(self, row: dict) -> list[tuple[str, str]]:
        out = []
        i = 1
        while f"code|{i}" in row:
            c, t = row.get(f"code|{i}", "").strip(), row.get(f"code|{i}|type", "").strip().upper()
            if c:
                out.append((c, t))
            i += 1
        return out

    def _wide_payers(self, row: dict) -> list[PayerCharge]:
        groups: dict[tuple[str, str], PayerCharge] = {}
        for col, val in row.items():
            parts = col.split("|")
            if len(parts) < 3:
                continue
            fld = None
            if parts[0] == "standard_charge" and len(parts) == 4 and parts[3] in PAYER_FIELDS:
                payer, plan, fld = parts[1], parts[2], parts[3]
            elif parts[0] in PAYER_FIELDS and len(parts) == 3:
                payer, plan, fld = parts[1], parts[2], parts[0]
            if fld is None or val is None or not val.strip():
                continue
            pc = groups.setdefault((payer, plan), PayerCharge(payer=payer, plan=plan))
            _set_payer_field(pc, fld, val)
        return [p for p in groups.values() if _payer_has_charge(p)]

    def items(self) -> Iterator[Item]:
        """Yield normalised items.  In tall layout, consecutive rows for the same
        item (same description+codes+setting+modifiers) are merged into one Item
        with many payers."""
        if self.header is None:
            self.read_header()
        with self._open() as f:
            r = csv.reader(f)
            next(r); next(r); next(r)
            cols = self.columns
            pending: Item | None = None
            pending_key = None
            for lineno, rec in enumerate(r, start=4):
                if not any(x.strip() for x in rec):
                    continue
                row = {c: (rec[i] if i < len(rec) else "") for i, c in enumerate(cols)}
                codes = self._code_pairs(row)
                base = dict(
                    description=row.get("description", "").strip(),
                    codes=codes,
                    setting=row.get("setting", "").strip().lower(),
                    billing_class=(row.get("billing_class") or "").strip().lower() or None,
                    modifiers=(row.get("modifiers") or "").strip() or None,
                    gross=_num(row.get("standard_charge|gross")),
                    discounted_cash=_num(row.get("standard_charge|discounted_cash")),
                    min_negotiated=_num(row.get("standard_charge|min")),
                    max_negotiated=_num(row.get("standard_charge|max")),
                    notes=(row.get("additional_generic_notes") or "").strip() or None,
                )
                if self.layout == "wide":
                    yield Item(**base, payers=self._wide_payers(row), source_line=lineno)
                    continue
                # tall
                pc = None
                payer, plan = row.get("payer_name", "").strip(), row.get("plan_name", "").strip()
                if payer or plan:
                    pc = PayerCharge(payer=payer, plan=plan)
                    for fld in PAYER_FIELDS:
                        key = f"standard_charge|{fld}" if fld.startswith("negotiated") or fld == "methodology" else fld
                        if fld == "additional_payer_notes":
                            continue
                        _set_payer_field(pc, fld, row.get(key, ""))
                    if not _payer_has_charge(pc):
                        pc = None
                key = (base["description"], tuple(codes), base["setting"], base["modifiers"], base["billing_class"],
                       base["gross"], base["discounted_cash"], base["notes"])
                if pending is not None and key == pending_key:
                    # merge: fill blanks, append payer
                    for k in ("gross", "discounted_cash", "min_negotiated", "max_negotiated", "notes"):
                        if getattr(pending, k) is None and base[k] is not None:
                            setattr(pending, k, base[k])
                    if pc:
                        pending.payers.append(pc)
                    continue
                if pending is not None:
                    yield pending
                pending = Item(**base, payers=[pc] if pc else [], source_line=lineno)
                pending_key = key
            if pending is not None:
                yield pending

    def lookup(self, code: str) -> list[Item]:
        return [it for it in self.items() if it.has_code(code)]


def _set_payer_field(pc: PayerCharge, fld: str, val: str) -> None:
    val = (val or "").strip()
    if not val:
        return
    if fld == "negotiated_dollar":
        pc.negotiated_dollar = _num(val)
    elif fld == "negotiated_percentage":
        pc.negotiated_percentage = _num(val)
    elif fld == "negotiated_algorithm":
        pc.negotiated_algorithm = val
    elif fld == "methodology":
        pc.methodology = val.lower()
    elif fld == "median_amount":
        pc.median_amount = _num(val)
    elif fld == "10th_percentile":
        pc.p10 = _num(val)
    elif fld == "90th_percentile":
        pc.p90 = _num(val)
    elif fld == "count":
        pc.count = val
    elif fld == "additional_payer_notes":
        pc.notes = val


def _payer_has_charge(p: PayerCharge) -> bool:
    return any(x is not None for x in (p.negotiated_dollar, p.negotiated_percentage, p.negotiated_algorithm))


# ---- fixtures ------------------------------------------------------------

def build_slice(path: Path | str, codes: Iterable[str], out: Path | str, source_url: str,
                fetched_on: str, sha256: str | None = None) -> dict:
    """Extract every item carrying one of `codes` (any code column) into a JSON
    fixture with the file header, source URL and fetch date.  This is the
    offline fallback for the live fetch; it is a verbatim subset of the file."""
    want = {c.strip().upper() for c in codes}
    m = MRF(path)
    h = m.read_header()
    rows = [it.to_dict() for it in m.items() if any(c.upper() in want for c, _ in it.codes)]
    out = Path(out)
    doc = {
        "source_url": source_url,
        "fetched_on": fetched_on,
        "raw_file": Path(path).name,
        "raw_sha256": sha256,
        "layout": m.layout,
        "header": {k: v for k, v in h.__dict__.items() if k != "raw"},
        "codes_requested": sorted(want),
        "items": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc


class Fixture:
    """Fast lookup over a committed slice."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.header = self.doc["header"]

    def lookup(self, code: str) -> list[dict]:
        code = code.strip().upper()
        return [it for it in self.doc["items"] if any(c["code"].upper() == code for c in it["codes"])]


# ---- CLI -----------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fairbill.mrf")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("header"); a.add_argument("path")
    a = sub.add_parser("lookup"); a.add_argument("path"); a.add_argument("code"); a.add_argument("--max", type=int, default=5)
    a = sub.add_parser("slice"); a.add_argument("path"); a.add_argument("out"); a.add_argument("--codes", required=True)
    a.add_argument("--url", required=True); a.add_argument("--fetched", default=date.today().isoformat())
    a.add_argument("--sha256")
    ns = ap.parse_args(argv)
    if ns.cmd == "header":
        print(json.dumps(MRF(ns.path).read_header().__dict__, indent=1))
    elif ns.cmd == "lookup":
        m = MRF(ns.path); m.read_header()
        n = 0
        for it in m.lookup(ns.code):
            print(json.dumps(it.to_dict(), indent=1)); n += 1
            if n >= ns.max:
                break
        print(f"# {n} item(s) shown for {ns.code} in {m.header.hospital_name} ({m.layout}, last_updated_on={m.header.last_updated_on})")
    elif ns.cmd == "slice":
        codes = [c for c in re.split(r"[,\s]+", Path(ns.codes).read_text().strip() if Path(ns.codes).exists() else ns.codes) if c]
        d = build_slice(ns.path, codes, ns.out, ns.url, ns.fetched, ns.sha256)
        print(f"wrote {ns.out}: {len(d['items'])} items for {len(codes)} codes; hospital={d['header']['hospital_name']} last_updated_on={d['header']['last_updated_on']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
