"""Strands tools for the audit Graph: hospital resolution, price-file lookup, code meaning.

Every tool returns JSON-serializable data and never raises on a miss; a miss is
{"found": false, ...} so a specialist agent can keep going.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from functools import lru_cache

from strands import tool

from fairbill.codes import describe
from fairbill.mrf import DATA_DIR, Fixture

REPO_ROOT = DATA_DIR.parent
HOSPITALS_PATH = DATA_DIR / "hospitals.json"
GALLERY_CODES_PATH = DATA_DIR / "gallery_codes.txt"


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


@lru_cache(maxsize=1)
def hospitals() -> list[dict]:
    return json.loads(HOSPITALS_PATH.read_text(encoding="utf-8"))["hospitals"]


def hospital_record(hospital_id: str) -> dict | None:
    for h in hospitals():
        if h["id"] == hospital_id:
            return h
    return None


@lru_cache(maxsize=8)
def fixture_for(hospital_id: str):
    rec = hospital_record(hospital_id)
    if rec is None:
        return None
    return Fixture(REPO_ROOT / rec["fixture"])


def _text(value) -> str:
    """Tools are called by a model: an argument may arrive as a number, None or a dict."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


# Words that carry no identity: two different hospitals share all of them.
_STOP_WORDS = {"hospital", "hospitals", "medical", "center", "centers", "health",
               "system", "systems", "regional", "of", "the"}
MATCH_THRESHOLD = 0.8


def _tokens(s: str) -> list[str]:
    return [t for t in _clean(s).split() if t]


def _weight(tok: str) -> float:
    return 0.25 if tok in _STOP_WORDS else 1.0


def _best_ratio(tok: str, others: list[str]) -> float:
    return max((SequenceMatcher(None, tok, o).ratio() for o in others), default=0.0)


def _side(a: list[str], b: list[str]) -> float:
    den = sum(_weight(t) for t in a)
    return sum(_weight(t) * _best_ratio(t, b) for t in a) / den if den else 0.0


def _similarity(q: list[str], c: list[str]) -> float:
    """Symmetric token-set similarity: every token on each side finds its best partner
    on the other, weighted so that shared filler words cannot carry a match."""
    if not q or not c:
        return 0.0
    return 0.5 * (_side(q, c) + _side(c, q))


def _hit(h: dict, score: float, how: str) -> dict:
    return {
        "found": True,
        "hospital_id": h["id"],
        "name": h["name"],
        "state": h["state"],
        "npis": h["npis"],
        "ein": h["ein"],
        "addresses": h["addresses"],
        "billing_address": h.get("billing_address"),
        "mrf_url": h["mrf_url"],
        "transparency_page": h["transparency_page"],
        "fixture": h["fixture"],
        "match_score": round(score, 3),
        "matched_by": how,
    }


def resolve(name_or_address) -> dict:
    """Pure-Python hospital resolution (also used directly by audit.py).

    Three ladders, in order: an NPI or EIN printed on the bill; the registry name, an
    alias or an address printed as a whole phrase; then fuzzy token-set similarity on
    names and aliases, which catches misspellings without letting a hospital Fairbill
    does not cover match the nearest one it does.
    """
    raw = name_or_address if isinstance(name_or_address, str) else ("" if name_or_address is None else str(name_or_address))
    q = _clean(raw)
    if not q:
        return {"found": False, "query": raw, "best_score": 0.0}

    qd = re.sub(r"[^0-9]", "", raw)
    if len(qd) >= 9:
        for h in hospitals():
            ids = [n for n in h["npis"] if n] + [h["ein"].replace("-", "")]
            if any(i and i in qd for i in ids):
                return _hit(h, 1.0, "npi_or_ein")

    padded = f" {q} "
    for h in hospitals():
        for cand in [h["name"], *h["aliases"], *h["addresses"], h.get("billing_address", "")]:
            c = _clean(cand)
            if not c:
                continue
            if c == q or (len(c.split()) >= 2 and f" {c} " in padded):
                return _hit(h, 1.0, "name_or_alias")

    qt = _tokens(q)
    best, best_score = None, 0.0
    for h in hospitals():
        for cand in [h["name"], *h["aliases"]]:
            s = _similarity(qt, _tokens(cand))
            if s > best_score:
                best, best_score = h, s
    if best is None or best_score < MATCH_THRESHOLD:
        return {"found": False, "query": raw, "best_score": round(best_score, 3)}
    return _hit(best, best_score, "name_similarity")


def file_rows(hospital_id: str, code: str, limit: int = 4) -> list[dict]:
    """Ranked FileRow dicts for one code in one hospital price file.

    Keeps rows that carry a gross price, de-duplicates identical price rows, and
    ranks plain-language descriptions first (the OU file carries both the CMS long
    description and abbreviated chargemaster wording for the same code).
    """
    fx = fixture_for(hospital_id)
    if fx is None:
        return []
    doc = fx.doc
    items = [it for it in fx.lookup(code) if it.get("gross") is not None]
    if not items:
        items = fx.lookup(code)
    seen, uniq = set(), []
    for it in items:
        key = (it.get("description", ""), it.get("gross"), it.get("discounted_cash"), it.get("setting"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq.sort(key=lambda it: (-len(it.get("description", "")), it.get("source_line", 0)))
    out = []
    for it in uniq[:limit]:
        out.append({
            "hospital_id": hospital_id,
            "code": code.strip().upper(),
            "description": it.get("description", ""),
            "setting": it.get("setting"),
            "gross": it.get("gross"),
            "discounted_cash": it.get("discounted_cash"),
            "source_line": it.get("source_line", 0),
            "source_url": doc["source_url"],
            "fetched_on": doc["fetched_on"],
        })
    return out


def row_by_source_line(hospital_id: str, code: str, source_line: int) -> dict | None:
    """Exact fixture row by (code, source_line); used by the validator."""
    fx = fixture_for(hospital_id)
    if fx is None:
        return None
    for it in fx.lookup(code):
        if it.get("source_line") == source_line:
            return {
                "hospital_id": hospital_id,
                "code": code.strip().upper(),
                "description": it.get("description", ""),
                "setting": it.get("setting"),
                "gross": it.get("gross"),
                "discounted_cash": it.get("discounted_cash"),
                "source_line": it["source_line"],
                "source_url": fx.doc["source_url"],
                "fetched_on": fx.doc["fetched_on"],
            }
    return None


@lru_cache(maxsize=1)
def gallery_codes() -> set[str]:
    if not GALLERY_CODES_PATH.exists():
        return set()
    return {c.upper() for c in GALLERY_CODES_PATH.read_text(encoding="utf-8").split()}


# ---- Strands tools -------------------------------------------------------

@tool
def hospital_resolve(name_or_address: str) -> dict:
    """Identify which hospital a bill belongs to and where its published price file lives.

    Matches a hospital name, alias, street address, NPI or EIN against the Fairbill
    registry. Returns found false when nothing matches closely enough.

    Args:
        name_or_address: Hospital name, address, NPI or EIN exactly as printed on the bill.

    Returns:
        dict with found, hospital_id, name, addresses, npis, ein, mrf_url, fixture, match_score.
    """
    return resolve(_text(name_or_address))


@tool
def mrf_lookup(hospital_id: str, code: str) -> dict:
    """Look a billing code up in that hospital's own published standard-charges file.

    Returns the hospital's posted gross charge and discounted cash price for the code,
    with the source line, file URL and fetch date so a finding can cite it.

    Args:
        hospital_id: Registry id, e.g. "norman_regional" or "ou_health_oumc".
        code: CPT/HCPCS code as printed, e.g. "85025" or "J7030".

    Returns:
        dict with found, hospital_id, code, and rows: a best-first list of price-file rows
        (description, setting, gross, discounted_cash, source_line, source_url, fetched_on).
        An empty rows list with found false means the code is not in the file.
    """
    hospital_id, code = _text(hospital_id), _text(code).strip().upper()
    try:
        rows = file_rows(hospital_id, code)
    except Exception as exc:
        return {"found": False, "hospital_id": hospital_id, "code": code, "rows": [], "error": str(exc)}
    return {"found": bool(rows), "hospital_id": hospital_id, "code": code, "rows": rows}


@tool
def code_lookup(code: str) -> dict:
    """Explain a CPT/HCPCS code in plain English and give its bundling relationships.

    Says what the code means, which family it belongs to, which component codes a panel
    already includes, which panels list this code as a component, and, for visit codes,
    where the code sits on the level ladder.

    Args:
        code: CPT or HCPCS code, e.g. "85025", "99285", "J1885".

    Returns:
        dict with found, code, meaning, family, includes, component_of, and for visit codes
        level, level_ladder, level_note. found is false when the code is unknown.
    """
    code = _text(code).strip().upper()
    try:
        out = describe(code)
        out["in_gallery_code_set"] = code in gallery_codes()
    except Exception as exc:
        return {"found": False, "code": code, "error": str(exc)}
    return out
