"""Reader: photo or PDF of a hospital bill -> Bill (schema.py), via Bedrock multimodal structured output.

    python -m fairbill.reader gallery/bills/bill_01_photo.jpg
    python -m fairbill.reader --bench
"""
from __future__ import annotations

import argparse
import json
import os
import re

import cv2
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from fairbill.schema import Bill

load_dotenv()
os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_PRIMARY = os.environ.get("MODEL_PRIMARY", "us.anthropic.claude-sonnet-4-6")
MODEL_FALLBACK = os.environ.get("MODEL_FALLBACK", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "_runs" / "2026-09-13_phase3_reader"

SYSTEM_PROMPT = """You are a careful transcriber of US hospital billing statements. You read the page and
report exactly what is printed on it. You never compute, correct, normalize, or infer a value.

Rules, in order of importance:
1. TRANSCRIBE, DO NOT REASON. Copy codes character for character (e.g. "85025", "J1885", "00812").
   Copy money as a decimal number (1,729.00 -> 1729.00). Never recompute a total, a discount, or a price,
   and never "fix" a value that looks internally inconsistent: if a discounted price is printed equal to
   the gross charge, report it equal.
2. ROW DISCIPLINE. The image may be a phone photo: rotated a few degrees, blurred, unevenly lit. Every cell
   of a row sits on that row's own baseline, which slopes with the rotation. Read one row at a time, left to
   right, following that baseline across the page; never take a value from the row above or below. Before
   you report a row, check that its amount is horizontally aligned with its own code and description.
3. ONE ROW OF THE CHARGE TABLE = ONE BillLine, in printed top-to-bottom order, line_no starting at 1.
   Count the printed rows first and emit exactly that many. If two rows carry the same code, description and
   amount, emit BOTH: never merge, deduplicate, or collapse repeated rows, and never repeat a row that is
   printed once. If the table continues in a second section (for example a facility section and a
   professional section), keep numbering continuously across sections and include every row from both.
   Do not emit a line for subtotal, total, discount, payment, adjustment or balance rows.
4. COLUMN MAPPING.
   - Self-pay layouts print columns like Date | Code | Description | Qty | Charge | Your price, or
     Description | CPT/HCPCS | Date | Units | Charges | Self-pay price.
     charge = the "Charge"/"Charges" column. patient_price = the "Your price"/"Self-pay price" column.
   - Insured layouts print Date | Code | Description | Billed | Allowed | Plan paid | You owe.
     charge = the "Billed" column. patient_price = the "You owe" column. Never put "You owe" in charge.
   - units = the Qty/Units column if printed, else 1.
5. code_type: "CPT" for a 5-digit numeric code, "HCPCS" for a letter followed by 4 digits (J1885, J7030),
   "RC" for a 3 or 4 digit revenue code, "DRG", "NDC", otherwise "unknown".
6. If a line or section names a rendering provider or physician group, put that exact name in that line's
   `provider`. If network wording is printed next to a line or its section (for example
   "OUT OF NETWORK: this provider is not contracted with your plan"), copy that wording verbatim into that
   line's `network_note`.
7. Header fields: hospital_name, hospital_address, patient_name, account_number, statement_date,
   service_date_start, service_date_end exactly as printed (dates as ISO YYYY-MM-DD when the printed date
   is unambiguous). payer = the insurance plan name as printed, or "Self-pay" when the statement says
   self-pay. visit_type = the kind of visit as printed (e.g. "Emergency", "Outpatient lab").
8. clinical_note: if the page prints a visit summary, reason-for-visit, or clinical narrative box, copy its
   full text into clinical_note. Otherwise leave it null.
9. fictional_label_present: true only if the page literally prints that the patient is fictional or that
   this is a demo statement.
10. total_charges, total_adjustments, patient_balance: from the totals block if printed, else null.
11. NEVER INVENT. If a required string is genuinely not printed anywhere on the page, write exactly
    "not printed". If an optional value is not printed, leave it null. Do not guess a hospital name from
    a logo you cannot read.
12. Crops are best effort and unscored. Set every crop to null unless you are confident of the pixel box.
    Accuracy of codes and amounts matters far more than crops; never spend care on a crop at their expense."""

TRANSCRIBE_PROMPT = """Transcribe this statement before structuring anything.

First write the header lines you can read (hospital, patient, account, dates, payer, visit type).
Then write the charge table verbatim: the column header row, then one line per printed data row, cells
separated by " | ", in printed order, ending with a line "ROWS: <n>" giving the number of data rows.
Follow each row's own baseline across the page; the photo may be rotated. Copy every digit exactly and do
not reorder, merge or drop rows. Then write the totals block and any visit-summary or network note verbatim.
Plain text only."""

STRUCTURE_PROMPT = """Now return the Bill structure, using exactly the rows you just transcribed: same count,
same order, same digits. Re-check each amount against the row it belongs to before you emit it."""

ONE_PASS_PROMPT = """Read this statement and return the Bill structure.

Work row by row before you emit anything: count the printed data rows of the charge table, follow each
row's own baseline across the page (the photo may be rotated), and copy every digit exactly. Emit exactly
as many BillLine entries as there are printed data rows, in printed order."""


def _make_model(model_id: str):
    from fairbill.config import make_model
    model = make_model(model=model_id, max_tokens=8192)
    assert model.get_config()["model_id"] == model_id, "config model ids drifted from reader ids"
    return model


# Bedrock caps images at 5 MB; a 2200 px long edge keeps a JPEG q90 page well under it.
# 1600 px was measured on 2026-09-14 (see _runs/2026-09-14_speed/exp/REPORT.md).
MAX_EDGE = int(os.environ.get("FAIRBILL_MAX_EDGE") or 2200)

# How the page is read. Measured on the gallery bench, 2026-09-14 (median of 6 bills):
#   two_pass  transcribe, then structure with the transcript in context   21.6 s, 6 of 6 exact
#   one_pass  one structured call                                         18.5 s, 6 of 6 exact
#   parallel  transcribe and structure at once, transcript as a check     17.8 s, 6 of 6 exact
# parallel is the default: same exactness, the fastest, and the row-count cross-check the
# one-pass mode gives up is kept. FAIRBILL_READ_MODE overrides it.
READ_MODE = os.environ.get("FAIRBILL_READ_MODE") or "parallel"


def _bounded_jpeg(img):
    """Resize so the long edge is at most MAX_EDGE and encode JPEG q90."""
    h, w = img.shape[:2]
    scale = MAX_EDGE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None


def _deskew(path: Path) -> Optional[bytes]:
    """Flatten a phone photo: find the page quadrilateral and warp it to a rectangle.

    A few degrees of rotation shifts the right-hand money columns by a full row height across a wide
    statement table, which makes row-to-value alignment genuinely ambiguous. This undoes the geometry only;
    no pixel content is invented. Returns PNG bytes, or None when no page quad is found.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = 900.0 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    page = max(contours, key=cv2.contourArea)
    frac = cv2.contourArea(page) / float(small.shape[0] * small.shape[1])
    if not 0.25 < frac < 0.99:  # too small to be the page, or the page already fills the frame
        return None
    peri = cv2.arcLength(page, True)
    quad = None
    for eps in (0.02, 0.03, 0.01, 0.04):
        approx = cv2.approxPolyDP(page, eps * peri, True)
        if len(approx) == 4:
            quad = approx.reshape(4, 2).astype("float32") / scale
            break
    if quad is None:
        return None
    ordered = np.zeros((4, 2), dtype="float32")
    total, diff = quad.sum(axis=1), np.diff(quad, axis=1).ravel()
    ordered[0] = quad[np.argmin(total)]   # top-left
    ordered[2] = quad[np.argmax(total)]   # bottom-right
    ordered[1] = quad[np.argmin(diff)]    # top-right
    ordered[3] = quad[np.argmax(diff)]    # bottom-left
    out_w = int(max(np.linalg.norm(ordered[1] - ordered[0]), np.linalg.norm(ordered[2] - ordered[3])))
    out_h = int(max(np.linalg.norm(ordered[3] - ordered[0]), np.linalg.norm(ordered[2] - ordered[1])))
    if out_w < 200 or out_h < 200:
        return None
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype="float32")
    flat = cv2.warpPerspective(img, cv2.getPerspectiveTransform(ordered, dst), (out_w, out_h))
    return _bounded_jpeg(flat)


def _image_block(path: Path, page: int = 1) -> tuple[dict, tuple[int, int]]:
    """Return a Strands image content block plus (width, height) in pixels. `page` is 1-based, PDFs only."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import fitz  # PyMuPDF: render the page at 200 dpi; an image block reads more reliably than a PDF block
        doc = fitz.open(path)
        pix = doc[page - 1].get_pixmap(dpi=200)
        data, fmt, size = pix.tobytes("png"), "png", (pix.width, pix.height)
        doc.close()
    else:
        import io

        from PIL import Image
        flat = _deskew(path)
        if flat is None:  # no page quad found: send the image as is, bounded to the size cap
            img = cv2.imread(str(path))
            if img is None:
                raise ValueError(f"unreadable image: {path.name}")
            flat = _bounded_jpeg(img)
        data, fmt = flat, "jpeg"
        with Image.open(io.BytesIO(flat)) as im:
            size = im.size
    return {"image": {"format": fmt, "source": {"bytes": data}}}, size


def _reader_agent(model_id: str):
    from strands import Agent
    from fairbill.config import retry_strategy

    # Retry budget per attempt: 3 attempts at 2 s then 4 s of backoff, about 6 s of waiting
    # plus inference. read_bill_with_meta runs this twice on the primary before it ever
    # reaches the fallback, so a single ThrottlingException costs a retry, never Haiku.
    return Agent(model=_make_model(model_id), system_prompt=SYSTEM_PROMPT, callback_handler=None,
                 retry_strategy=retry_strategy())


def _size_note(w: int, h: int) -> str:
    return (f" The page image is {w} pixels wide and {h} pixels tall; any crop box must be "
            "pixel coordinates in that image with origin at the top-left.")


def _rows_declared(transcript: str) -> Optional[int]:
    m = re.findall(r"ROWS:\s*(\d+)", transcript or "")
    return int(m[-1]) if m else None


def _check(bill: Optional[Bill]) -> Bill:
    if bill is None:
        raise RuntimeError("model returned no structured output")
    if not any(ln.code for ln in bill.lines):
        raise NotABillError("no billing codes found on the page; this does not look like a hospital bill")
    # line_no is the page's row key: the audit maps findings by it and the page draws one
    # row per number. A model that numbers two rows the same would make one row vanish and
    # another draw twice, so rows are renumbered in printed order. Nothing is dropped: an
    # identical row twice is a duplicate charge, which the audit is there to find.
    if len({ln.line_no for ln in bill.lines}) != len(bill.lines):
        for i, ln in enumerate(bill.lines, start=1):
            ln.line_no = i
    return bill


def _extract(path: Path, model_id: str, page: int = 1, prepared=None) -> Bill:
    block, (w, h) = prepared or _image_block(path, page)
    if READ_MODE == "one_pass":
        agent = _reader_agent(model_id)
        result = agent([{"text": ONE_PASS_PROMPT + _size_note(w, h)}, block],
                       structured_output_model=Bill)
        return _check(result.structured_output)
    if READ_MODE == "parallel":
        return _extract_parallel(model_id, block, w, h)

    agent = _reader_agent(model_id)
    # Pass 1: free-form verbatim transcription, which keeps row/value alignment on a rotated photo.
    agent([{"text": TRANSCRIBE_PROMPT}, block])
    # Pass 2: structure it, with the image and the transcription both in context.
    result = agent(STRUCTURE_PROMPT + _size_note(w, h), structured_output_model=Bill)
    return _check(result.structured_output)


def _extract_parallel(model_id: str, block: dict, w: int, h: int) -> Bill:
    """Transcribe and structure at the same time instead of one after the other.

    The transcription is no longer an input to the structuring pass, so it is used as a
    check: when the two disagree on the row count, a reconcile pass settles it with both
    the transcript and the image in context.
    """
    from concurrent.futures import ThreadPoolExecutor

    scribe = _reader_agent(model_id)

    def transcribe():
        return str(scribe([{"text": TRANSCRIBE_PROMPT}, block]))

    def structure():
        a = _reader_agent(model_id)
        return a([{"text": ONE_PASS_PROMPT + _size_note(w, h)}, block],
                 structured_output_model=Bill).structured_output

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_t, f_s = pool.submit(transcribe), pool.submit(structure)
        transcript, bill = f_t.result(), f_s.result()
    declared = _rows_declared(transcript)
    if bill is not None and declared is not None and declared != len(bill.lines):
        bill = scribe(STRUCTURE_PROMPT + _size_note(w, h), structured_output_model=Bill).structured_output
    return _check(bill)


class NotABillError(ValueError):
    pass


def _fatal(exc: Exception) -> bool:
    """Errors a retry cannot fix: bad input, invalid request, our own validation."""
    name = type(exc).__name__
    return isinstance(exc, (NotABillError, FileNotFoundError, ValueError)) or "ValidationException" in name


def read_bill_with_meta(image_or_pdf_path, page: int = 1) -> tuple[Bill, dict]:
    """read_bill plus {model, attempts, seconds, notes}."""
    path = Path(image_or_pdf_path)
    if not path.exists():
        raise FileNotFoundError(path)
    from fairbill.config import down_reason, ladder, live_ladder, mark_down, model_name, probe
    # the one-token probe of the top rung runs while the image is prepared, so a rung that
    # hit its daily cap is skipped before any vision pass starts, at no cost to a healthy day
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        probing = pool.submit(probe, ladder()[0])
        block = _image_block(path, page)
        probing.result()
    rungs = live_ladder()
    primary = ladder()[0]  # the note names the configured primary even when the probe skipped it
    # the top live rung gets two whole tries, every lower rung one; a rung that reports a
    # throttle is dropped from the plan at once instead of being tried again
    plan = [rungs[0], rungs[0], *rungs[1:]]
    meta = {"model": primary, "attempts": 0, "seconds": 0.0, "notes": []}
    t0 = time.time()
    last: Optional[Exception] = None
    while plan:
        model_id = plan.pop(0)
        meta["attempts"] += 1
        meta["model"] = model_id
        try:
            bill = _extract(path, model_id, page, block)
            meta["seconds"] = round(time.time() - t0, 2)
            if model_id != primary:
                why = down_reason(primary) or "failed twice"
                meta["notes"].append(f"{model_name(primary)} {why}; read with {model_name(model_id)}")
            return bill, meta
        except Exception as exc:  # retry once on the primary, then step down the ladder
            last = exc
            meta["notes"].append(f"attempt {meta['attempts']} on {model_id} failed: {type(exc).__name__}: {str(exc)[:160]}")
            if _fatal(exc):
                break
            if mark_down(model_id, exc):
                plan = [m for m in plan if m != model_id]
                continue
            time.sleep(2 * meta["attempts"])
    meta["seconds"] = round(time.time() - t0, 2)
    raise RuntimeError(f"read_bill failed after {meta['attempts']} attempts: {last}")


def read_bill(image_or_pdf_path, page: int = 1) -> Bill:
    """Read a hospital bill image or PDF and return the extracted Bill. `page` is 1-based (PDFs only)."""
    bill, _ = read_bill_with_meta(image_or_pdf_path, page)
    return bill


# ---------------------------------------------------------------- bench

def _norm_code(v) -> str:
    return str(v or "").strip().upper().replace(" ", "")


def _money(v) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _bench_one(entry: dict, got: Bill) -> list[dict]:
    """Per-field diffs between truth entry['bill'] and the extraction. Empty list = exact."""
    truth = entry["bill"]
    diffs: list[dict] = []

    def miss(line_no, field, exp, act):
        diffs.append({"line_no": line_no, "field": field, "expected": exp, "got": act})

    th, gh = str(truth.get("hospital_name", "")).casefold(), got.hospital_name.casefold()
    if not (th and gh and (th in gh or gh in th)):
        miss(None, "hospital_name", truth.get("hospital_name"), got.hospital_name)
    tp, gp = str(truth.get("payer", "")).strip().casefold(), got.payer.strip().casefold()
    if tp != gp:
        miss(None, "payer", truth.get("payer"), got.payer)

    t_lines, g_lines = truth.get("lines", []), got.lines
    if len(t_lines) != len(g_lines):
        miss(None, "line_count", len(t_lines), len(g_lines))
    for i, tl in enumerate(t_lines):
        if i >= len(g_lines):
            break
        gl = g_lines[i]
        if _norm_code(tl.get("code")) != _norm_code(gl.code):
            miss(i + 1, "code", tl.get("code"), gl.code)
        if _money(tl.get("charge")) != _money(gl.charge):
            miss(i + 1, "charge", tl.get("charge"), gl.charge)
        if tl.get("patient_price") is not None and _money(tl["patient_price"]) != _money(gl.patient_price):
            miss(i + 1, "patient_price", tl.get("patient_price"), gl.patient_price)
    return diffs


def run_bench() -> int:
    gallery = ROOT / "gallery"
    truth_path = gallery / "truth.json"
    if not truth_path.exists():
        print(f"gallery truth not found at {truth_path}; bench cannot run", file=sys.stderr)
        return 2
    entries = json.loads(truth_path.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("bills") or entries.get("entries") or list(entries.values())

    rows, extractions = [], {}
    for entry in entries:
        if "bill" not in entry:
            print(f"unexpected truth entry shape, keys: {sorted(entry)}", file=sys.stderr)
            return 2
        bill_id = entry.get("id")
        photo = (entry.get("files") or {}).get("photo")
        path = (ROOT / photo) if photo else (gallery / "bills" / f"bill_{bill_id}_photo.jpg")
        if not Path(path).exists():
            rows.append({"id": bill_id, "status": "missing photo", "diffs": [], "seconds": 0.0, "model": "-"})
            continue
        try:
            bill, meta = read_bill_with_meta(path)
            diffs = _bench_one(entry, bill)
            rows.append({
                "id": bill_id, "status": "exact" if not diffs else f"{len(diffs)} diff(s)",
                "diffs": diffs, "seconds": meta["seconds"], "model": meta["model"],
                "notes": meta["notes"], "lines": len(bill.lines),
            })
            extractions[str(bill_id)] = bill.model_dump()
        except Exception as exc:
            rows.append({"id": bill_id, "status": f"error: {exc}", "diffs": [], "seconds": 0.0, "model": "-"})

    exact = sum(1 for r in rows if r["status"] == "exact")
    print(f"{'bill':<10}{'lines':>6}{'sec':>8}  {'model':<44}status")
    for r in rows:
        print(f"{str(r['id']):<10}{str(r.get('lines', '-')):>6}{r['seconds']:>8.1f}  {r['model'][:42]:<44}{r['status']}")
    for r in rows:
        for d in r["diffs"]:
            where = f"line {d['line_no']}" if d["line_no"] else "header"
            print(f"  MISS {r['id']} {where} {d['field']}: expected {d['expected']!r}, got {d['got']!r}")
    print(f"\n{exact} of {len(rows)} bills exact")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "bench.json").write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model_primary": MODEL_PRIMARY, "model_fallback": MODEL_FALLBACK, "region": REGION,
        "exact": exact, "total": len(rows), "rows": rows,
        "comparison": "code (upper/stripped), charge and patient_price at 2dp, line count, "
                      "payer (casefold) and hospital_name (casefold containment either way)",
    }, indent=2), encoding="utf-8")
    (RUN_DIR / "bench_extractions.json").write_text(json.dumps(extractions, indent=2), encoding="utf-8")
    print(f"wrote {RUN_DIR / 'bench.json'}")
    return 0 if rows and exact == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m fairbill.reader")
    ap.add_argument("path", nargs="?", help="image or PDF of a bill")
    ap.add_argument("--bench", action="store_true", help="run the gallery bench")
    ap.add_argument("--page", type=int, default=1, help="1-based page of a multi-page PDF (default 1)")
    args = ap.parse_args()
    if args.bench:
        return run_bench()
    if not args.path:
        ap.error("give a path or --bench")
    bill, meta = read_bill_with_meta(args.path, args.page)
    print(json.dumps(bill.model_dump(), indent=2))
    print(json.dumps({"model": meta["model"], "seconds": meta["seconds"], "notes": meta["notes"]}, indent=2),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
