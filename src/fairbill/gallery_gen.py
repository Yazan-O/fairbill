"""Deterministic builder for the Fairbill demo gallery (gallery/SPEC.md).

Six fictional patient statements whose every price is a real row of a real hospital
standard-charges file. The bill tables below are the spec's tables, encoded as data.
Each row is looked up in the fixture by (code, source_line) and its gross /
discounted_cash are asserted against the spec: a mismatch is a hard failure, never a
silent correction.

  python -m fairbill.gallery_gen           rebuild PDFs, page PNGs, photos, truth.json
  python -m fairbill.gallery_gen --check   re-verify truth.json against the fixtures
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from fairbill.mrf import Fixture
from fairbill.schema import Bill, BillLine, Crop, FileRow, Finding

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
GALLERY = ROOT / "gallery"
BILLS_DIR = GALLERY / "bills"
DPI = 200
PAGE_W, PAGE_H = letter  # 612 x 792 pt
SEED = 20260913

FICTION_LABEL = ("DEMO STATEMENT. Patient is fictional. Prices are the hospital's own "
                 "published standard charges (fetched 2026-09-13).")
_REG = {h["id"]: h for h in json.loads((DATA / "hospitals.json").read_text(encoding="utf-8"))["hospitals"]}
NR_ADDR = _REG["norman_regional"]["billing_address"]
OU_ADDR = "700 NE 13th St, Oklahoma City, OK 73104"
ADDR_NORMAN = "123 Demo Street, Norman, OK 73069"
ADDR_OKC = "456 Demo Avenue, Oklahoma City, OK 73104"

# ---------------------------------------------------------------- bill tables
# line tuples: (code, source_line, description printed on the bill, charge, your price[, the
# fixture's discounted_cash when the bill deliberately prints something else in "your price"])
BILLS: list[dict] = [
    {
        "id": "bill_01", "hospital_id": "norman_regional", "layout": "norman",
        "patient": "Jordan Sample", "patient_address": ADDR_NORMAN,
        "payer": "Self-pay", "visit_type": "Emergency visit", "service_date": "2026-08-14",
        "lines": [
            ("99283", 16377, "ED Services Level 3", 1729.00, 1037.40),
            ("36415", 16614, "ED Venipuncture, Routine", 44.00, 26.40),
            ("85025", 13220, "Complete Blood Count (CBC)", 140.00, 84.00),
            ("85025", 13220, "Complete Blood Count (CBC)", 140.00, 84.00),
            ("80053", 10980, "Comprehensive Metabolic Panel", 275.00, 165.00),
            ("71046", 14756, "Chest X-ray, 2 views", 449.00, 269.40),
            ("J1885", 5378, "Ketorolac 15 mg injection", 48.82, 29.29),
            ("96372", 5911, "Therapeutic injection, IM", 221.00, 132.60),
        ],
        "planted": [{
            "kind": "duplicate", "line_nos": [3, 4], "amount_at_stake": 84.00, "basis": "cash",
            "summary": "The complete blood count (CPT 85025) is billed twice on the same day; "
                       "only one collection is documented, so one of the two charges should come off.",
            "evidence": [("85025", 13220)],
        }],
    },
    {
        "id": "bill_02", "hospital_id": "norman_regional", "layout": "norman",
        "patient": "Casey Example", "patient_address": ADDR_NORMAN,
        "payer": "Self-pay", "visit_type": "Outpatient diagnostics", "service_date": "2026-07-22",
        "lines": [
            ("36415", 13718, "Venipuncture", 46.00, 27.60),
            ("84443", 10950, "Thyroid Stimulating Hormone", 248.00, 148.80),
            ("83036", 12018, "Hemoglobin A1c", 135.00, 81.00),
            ("93005", 17430, "ECG tracing, 12 lead", 409.00, 245.40),
            ("71046", 14756, "Chest X-ray, 2 views", 449.00, 449.00, 269.40),
        ],
        "planted": [{
            "kind": "above_cash", "line_nos": [5], "amount_at_stake": 179.60, "basis": "cash",
            "summary": "The chest X-ray is charged at the full gross price; the hospital's own file "
                       "lists a $269.40 self-pay price for the same code, a $179.60 difference.",
            "evidence": [("71046", 14756)],
        }],
    },
    {
        "id": "bill_03", "hospital_id": "ou_health_oumc", "layout": "ou",
        "patient": "Riley Demo", "patient_address": ADDR_OKC,
        "payer": "Self-pay", "visit_type": "Outpatient lab", "service_date": "2026-08-03",
        "lines": [
            ("36415", 4452, "Collection of venous blood, venipuncture", 90.00, 9.00),
            ("85027", 6848, "Complete blood cell count, automated", 257.00, 25.70),
            ("85018", 4038, "Hemoglobin", 113.00, 11.30),
            ("85014", 4764, "Red blood cell concentration (hematocrit)", 123.00, 12.30),
            ("85007", 4307, "Microscopic exam, white cells, manual differential", 636.00, 63.60),
            ("80053", 6886, "Blood test, comprehensive group of chemicals", 1025.00, 102.50),
        ],
        "planted": [{
            "kind": "unbundled", "line_nos": [2, 3, 4, 5], "amount_at_stake": 75.80, "basis": "cash",
            "summary": "Four lines are the components of one complete blood count with differential "
                       "(CPT 85025), which this hospital prices as a single row at $37.10 self-pay.",
            "evidence": [("85025", 6833), ("85027", 6848), ("85018", 4038), ("85014", 4764), ("85007", 4307)],
        }],
    },
    {
        "id": "bill_04", "hospital_id": "ou_health_oumc", "layout": "ou",
        "patient": "Morgan Placeholder", "patient_address": ADDR_OKC,
        "payer": "Self-pay", "visit_type": "Emergency visit", "service_date": "2026-08-21",
        "clinical_note": ("Reason for visit: sore throat and cough, 2 days. Vitals normal, no fever. "
                          "Rapid strep negative. Chest X-ray clear. Discharged home with instructions; "
                          "no prescriptions."),
        "lines": [
            ("99285", 4045, "Emergency department visit, life-threatening or high severity", 6795.00, 679.50),
            ("36415", 4452, "Collection of venous blood, venipuncture", 90.00, 9.00),
            ("80053", 6886, "Blood test, comprehensive group of chemicals", 1025.00, 102.50),
            ("71046", 4603, "X-ray of chest, 2 views", 683.00, 68.30),
            ("93005", 3949, "Routine electrocardiogram, 12 leads, tracing", 532.00, 53.20),
            ("J7030", 4341, "Normal saline solution infusion", 7.00, 0.70),
        ],
        "planted": [{
            "kind": "upcoded", "line_nos": [1], "amount_at_stake": 489.70, "basis": "cash",
            "needs_user_judgment": True,
            "summary": "The visit note describes a low-complexity visit, but the bill uses the highest "
                       "emergency level (99285); the moderate level (99283) is $189.80 self-pay here.",
            "evidence": [("99285", 4045), ("99283", 4688)],
        }],
    },
    {
        "id": "bill_05", "hospital_id": "norman_regional", "layout": "insured",
        "patient": "Taylor Fictional", "patient_address": ADDR_NORMAN,
        "payer": "Sooner Plains Health Plan (fictional)", "visit_type": "Outpatient procedure",
        "service_date": "2026-06-10",
        # (code, source_line|None, description, billed, allowed, plan_paid, you_owe, section)
        "insured_lines": [
            ("45378", 15227, "Colonoscopy, diagnostic", 2506.00, 1800.00, 1440.00, 360.00, "facility"),
            ("J7030", 5825, "Sodium chloride 1000 mL bag", 86.66, 60.00, 48.00, 12.00, "facility"),
            ("00812", None, "Anesthesia for screening colonoscopy", 1850.00, 0.00, 0.00, 1850.00, "professional"),
        ],
        "professional_provider": "Red River Anesthesia Associates (fictional)",
        "network_note": "OUT OF NETWORK: this provider is not contracted with your plan",
        "planted": [{
            "kind": "out_of_network", "line_nos": [3], "amount_at_stake": 1850.00, "basis": "gross",
            "regulation": ("No Surprises Act, 45 CFR 149.420 (non-emergency services by nonparticipating "
                           "providers at participating facilities; anesthesiology cannot be balance-billed)"),
            "summary": "Anesthesia delivered at an in-network facility is protected by the No Surprises Act; "
                       "the patient owes at most the in-network cost share, not the $1,850.00 balance bill.",
            "evidence": [],
        }],
    },
    {
        "id": "bill_06", "hospital_id": "norman_regional", "layout": "norman",
        "patient": "Alex Specimen", "patient_address": ADDR_NORMAN,
        "payer": "Self-pay", "visit_type": "Outpatient lab", "service_date": "2026-07-08",
        "lines": [
            ("36415", 13718, "Venipuncture", 46.00, 27.60),
            ("85025", 13220, "Complete Blood Count (CBC)", 140.00, 84.00),
            ("80053", 10980, "Comprehensive Metabolic Panel", 275.00, 165.00),
            ("84443", 10950, "Thyroid Stimulating Hormone", 248.00, 148.80),
            ("81001", 10912, "Urinalysis, automated with microscopy", 164.00, 98.40),
        ],
        "planted": [],
    },
]


# ---------------------------------------------------------------- fixtures
class Hospitals:
    def __init__(self) -> None:
        reg = json.loads((DATA / "hospitals.json").read_text(encoding="utf-8"))
        self.by_id = {h["id"]: h for h in reg["hospitals"]}
        self.fixtures = {hid: Fixture(ROOT / h["fixture"]) for hid, h in self.by_id.items()}

    def row(self, hospital_id: str, code: str, source_line: int) -> dict:
        """The one fixture item carrying `code` on `source_line`. Missing is fatal."""
        fx = self.fixtures[hospital_id]
        hits = [it for it in fx.lookup(code) if it["source_line"] == source_line]
        if len(hits) != 1:
            raise SystemExit(f"FATAL: {hospital_id} code {code} line {source_line}: "
                             f"{len(hits)} fixture rows, expected 1")
        return hits[0]

    def checked_row(self, hospital_id: str, code: str, source_line: int,
                    gross: float, cash: float | None) -> dict:
        it = self.row(hospital_id, code, source_line)
        if abs(it.get("gross", -1) - gross) > 0.005:
            raise SystemExit(f"FATAL: {hospital_id} {code}@{source_line} gross "
                             f"{it.get('gross')} != spec {gross}")
        if cash is not None and abs(it.get("discounted_cash", -1) - cash) > 0.005:
            raise SystemExit(f"FATAL: {hospital_id} {code}@{source_line} discounted_cash "
                             f"{it.get('discounted_cash')} != spec {cash}")
        return it

    def file_row(self, hospital_id: str, code: str, source_line: int) -> FileRow:
        it = self.row(hospital_id, code, source_line)
        fx = self.fixtures[hospital_id]
        return FileRow(hospital_id=hospital_id, code=code, description=it["description"],
                       setting=it.get("setting"), gross=it.get("gross"),
                       discounted_cash=it.get("discounted_cash"), source_line=source_line,
                       source_url=fx.doc["source_url"], fetched_on=fx.doc["fetched_on"])


def code_type(code: str) -> str:
    if code.isdigit() and len(code) == 5:
        return "CPT"
    if code[0] in "JG" and code[1:].isdigit():
        return "HCPCS"
    return "unknown"


def money(v: float) -> str:
    return f"{v:,.2f}"


def add_days(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- drawing
class Sheet:
    """reportlab canvas wrapper that records, for every value it draws, the pixel box
    that value occupies on the 200 dpi page render."""

    def __init__(self, path: Path):
        self.c = rl_canvas.Canvas(str(path), pagesize=letter)
        self.crops: dict[str, Crop] = {}
        self.line_crops: dict[int, Crop] = {}

    # pt (origin bottom-left) -> px on the 200 dpi PNG (origin top-left)
    def _crop(self, x0: float, y0: float, x1: float, y1: float) -> Crop:
        s = DPI / 72.0
        return Crop(page=0, x0=int(x0 * s), y0=int((PAGE_H - y1) * s),
                    x1=int(round(x1 * s)), y1=int(round((PAGE_H - y0) * s)))

    def text(self, x: float, y: float, s: str, font: str = "Helvetica", size: float = 9,
             key: str | None = None, align: str = "left", width: float | None = None):
        self.c.setFont(font, size)
        w = self.c.stringWidth(s, font, size)
        if align == "right":
            self.c.drawRightString(x, y, s)
            x0, x1 = x - w, x
        else:
            self.c.drawString(x, y, s)
            x0, x1 = x, x + w
        if key:
            self.crops[key] = self._crop(x0 - 2, y - size * 0.30 - 2,
                                         (x0 + width if width else x1) + 2, y + size * 0.82 + 2)
        return w

    def fit(self, s: str, font: str, size: float, width: float, floor: float = 6.5) -> float:
        while size > floor and self.c.stringWidth(s, font, size) > width:
            size -= 0.25
        return size

    def row_box(self, line_no: int, x0: float, y: float, x1: float, size: float):
        self.line_crops[line_no] = self._crop(x0 - 2, y - size * 0.45, x1 + 2, y + size * 1.05)

    def banner(self, top_y: float, bottom_y: float):
        for y, key in ((top_y, "fictional_label_top"), (bottom_y, "fictional_label_bottom")):
            self.c.setFont("Helvetica-Oblique", 7.6)
            w = self.c.stringWidth(FICTION_LABEL, "Helvetica-Oblique", 7.6)
            x = (PAGE_W - w) / 2
            self.c.drawString(x, y, FICTION_LABEL)
            self.crops[key] = self._crop(x - 2, y - 3, x + w + 2, y + 8)

    def save(self):
        self.c.save()


def notice(sh: Sheet, x: float, y_top: float, w: float, rows: list[str],
           family: str = "Helvetica") -> None:
    """Boilerplate message block; fills the band a real statement leaves for notices."""
    plain = "Times-Roman" if family == "Times" else family
    bold = "Times-Bold" if family == "Times" else family + "-Bold"
    h = 16 + 12 * len(rows)
    sh.c.setLineWidth(0.6)
    sh.c.rect(x, y_top - h, w, h, stroke=1, fill=0)
    yy = y_top - 18
    for i, r in enumerate(rows):
        sh.text(x + 10, yy, r, bold if i == 0 else plain, 8.5)
        yy -= 12


def draw_norman(sh: Sheet, b: dict, hosp: dict, lines: list[dict], totals: dict) -> None:
    c = sh.c
    sh.banner(762, 34)
    c.setFont("Helvetica-Bold", 15)
    sh.text(54, 726, hosp["name"], "Helvetica-Bold", 15, key="hospital_name")
    sh.text(54, 712, NR_ADDR, "Helvetica", 7.8, key="hospital_address")
    sh.text(54, 700, "Patient billing questions: 405-555-0101 (fictional)", "Helvetica", 7.8)
    sh.text(558, 726, "PATIENT STATEMENT", "Helvetica-Bold", 13, align="right")

    c.setLineWidth(0.8)
    c.line(54, 690, 558, 690)

    y = 674
    sh.text(54, y, "Patient", "Helvetica-Bold", 8)
    sh.text(54, y - 12, b["patient"], "Helvetica", 9.5, key="patient_name")
    sh.text(54, y - 24, b["patient_address"], "Helvetica", 8.2)
    sh.text(310, y, "Account number", "Helvetica-Bold", 8)
    sh.text(310, y - 12, b["account"], "Helvetica", 9.5, key="account_number")
    sh.text(440, y, "Statement date", "Helvetica-Bold", 8)
    sh.text(440, y - 12, b["statement_date"], "Helvetica", 9.5, key="statement_date")
    sh.text(310, y - 30, "Dates of service", "Helvetica-Bold", 8)
    sh.text(310, y - 42, b["service_date"], "Helvetica", 9.5, key="service_date_start")
    sh.text(440, y - 30, "Payer", "Helvetica-Bold", 8)
    sh.text(440, y - 42, b["payer"], "Helvetica", 9.5, key="payer")
    sh.text(54, y - 42, f"Visit type: {b['visit_type']}", "Helvetica", 8.5)

    # table
    cols = [(58, "Date", "left"), (118, "Code", "left"), (168, "Description", "left"),
            (392, "Qty", "right"), (462, "Charge", "right"), (552, "Your price", "right")]
    ty = 600
    c.setFillColorRGB(0.90, 0.90, 0.90)
    c.rect(54, ty - 4, 504, 16, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    for x, label, align in cols:
        sh.text(x, ty, label, "Helvetica-Bold", 8.5, align=align)

    y = ty - 20
    for ln in lines:
        size = sh.fit(ln["description"], "Helvetica", 9, 215)
        sh.text(58, y, ln["date"], "Helvetica", 8.5)
        sh.text(118, y, ln["code"], "Helvetica", 9)
        sh.text(168, y, ln["description"], "Helvetica", size)
        sh.text(392, y, f"{int(ln['units'])}", "Helvetica", 9, align="right")
        sh.text(462, y, money(ln["charge"]), "Helvetica", 9, align="right")
        sh.text(552, y, money(ln["patient_price"]), "Helvetica", 9, align="right")
        sh.row_box(ln["line_no"], 54, y, 558, 9)
        c.setStrokeColorRGB(0.82, 0.82, 0.82)
        c.setLineWidth(0.3)
        c.line(54, y - 5, 558, y - 5)
        y -= 17

    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.8)
    y -= 6
    c.line(340, y + 8, 558, y + 8)
    for label, val, key, bold in (
            ("Total charges", totals["total_charges"], "total_charges", False),
            ("Self-pay discount", -totals["total_adjustments"], "total_adjustments", False),
            ("Amount due", totals["patient_balance"], "patient_balance", True)):
        f = "Helvetica-Bold" if bold else "Helvetica"
        sh.text(344, y, label, f, 9.5)
        sh.text(552, y, money(val), f, 9.5, align="right", key=key)
        y -= 15

    sh.text(54, y + 6, "Self-pay discount applied per hospital financial assistance policy.",
            "Helvetica-Oblique", 8)
    sh.text(54, y - 8, f"Payment due by {b['due_date']}.", "Helvetica", 8.5, key="due_date")

    notice(sh, 54, 210, 504,
           ["MESSAGES", "Questions about this statement? Call Patient Financial Services.",
            "Financial assistance and payment plans are available on request.",
            "An itemized statement is available at no charge."])

    # payment stub
    c.setDash(3, 3)
    c.setLineWidth(0.6)
    c.line(54, 132, 558, 132)
    c.setDash()
    c.rect(54, 52, 504, 70, stroke=1, fill=0)
    sh.text(64, 106, "PAYMENT STUB - return with payment", "Helvetica-Bold", 8.5)
    sh.text(64, 92, f"Account {b['account']}", "Helvetica", 8.5)
    sh.text(64, 80, f"Patient {b['patient']}", "Helvetica", 8.5)
    sh.text(64, 68, "Pay online at the hospital's billing portal", "Helvetica", 8.5)
    sh.text(548, 92, "Amount due", "Helvetica-Bold", 9, align="right")
    sh.text(548, 76, money(totals["patient_balance"]), "Helvetica-Bold", 12, align="right")


def draw_ou(sh: Sheet, b: dict, hosp: dict, lines: list[dict], totals: dict) -> None:
    c = sh.c
    sh.banner(764, 32)
    sh.text(50, 730, hosp["name"], "Times-Bold", 16, key="hospital_name")
    sh.text(50, 716, OU_ADDR, "Times-Roman", 8, key="hospital_address")
    sh.text(50, 705, "Patient Financial Services  |  405-555-0102 (fictional)", "Times-Roman", 8)
    c.setLineWidth(2)
    c.line(50, 697, 562, 697)
    c.setLineWidth(0.6)
    c.line(50, 694, 562, 694)
    sh.text(562, 730, "Statement of Account", "Times-Bold", 13, align="right")

    y = 676
    for label, val, key in (("PATIENT", b["patient"], "patient_name"),
                            ("ACCOUNT", b["account"], "account_number"),
                            ("STATEMENT DATE", b["statement_date"], "statement_date")):
        sh.text(50, y, label, "Times-Bold", 7.5)
        sh.text(140, y, val, "Times-Roman", 9.5, key=key)
        y -= 13
    sh.text(50, y, "ADDRESS", "Times-Bold", 7.5)
    sh.text(140, y, b["patient_address"], "Times-Roman", 9)
    y -= 13
    sh.text(50, y, "SERVICE DATE", "Times-Bold", 7.5)
    sh.text(140, y, b["service_date"], "Times-Roman", 9.5, key="service_date_start")
    sh.text(330, y, "FINANCIAL CLASS", "Times-Bold", 7.5)
    sh.text(440, y, b["payer"], "Times-Roman", 9.5, key="payer")
    y -= 13
    sh.text(50, y, "VISIT TYPE", "Times-Bold", 7.5)
    sh.text(140, y, b["visit_type"], "Times-Roman", 9.5)

    if b.get("clinical_note"):
        c.rect(50, y - 66, 512, 58, stroke=1, fill=0)
        sh.text(58, y - 24, "VISIT SUMMARY (fictional)", "Times-Bold", 8)
        words, row, ny = b["clinical_note"].split(), "", y - 38
        top = ny
        for w in words:
            trial = (row + " " + w).strip()
            if c.stringWidth(trial, "Times-Roman", 8.8) > 494:
                sh.text(58, ny, row, "Times-Roman", 8.8)
                ny -= 11
                row = w
            else:
                row = trial
        sh.text(58, ny, row, "Times-Roman", 8.8)
        sh.crops["clinical_note"] = sh._crop(56, ny - 4, 556, top + 9)
        y -= 74

    ty = y - 26
    cols = [(50, "Description", "left"), (296, "CPT/HCPCS", "left"), (366, "Date", "left"),
            (444, "Units", "right"), (506, "Charges", "right"), (562, "Self-pay price", "right")]
    for x, label, align in cols:
        sh.text(x, ty, label, "Times-Bold", 8, align=align)
    c.setLineWidth(0.8)
    c.line(50, ty - 5, 562, ty - 5)

    ly = ty - 20
    for ln in lines:
        size = sh.fit(ln["description"], "Times-Roman", 9, 238)
        sh.text(50, ly, ln["description"], "Times-Roman", size)
        sh.text(296, ly, ln["code"], "Times-Roman", 9)
        sh.text(366, ly, ln["date"], "Times-Roman", 8.5)
        sh.text(444, ly, f"{int(ln['units'])}", "Times-Roman", 9, align="right")
        sh.text(506, ly, money(ln["charge"]), "Times-Roman", 9, align="right")
        sh.text(562, ly, money(ln["patient_price"]), "Times-Roman", 9, align="right")
        sh.row_box(ln["line_no"], 50, ly, 562, 9)
        c.setStrokeColorRGB(0.82, 0.82, 0.82)
        c.setLineWidth(0.3)
        c.line(50, ly - 5, 562, ly - 5)
        ly -= 17
    c.setLineWidth(0.5)
    c.line(50, ly + 7, 562, ly + 7)

    # summary box at right
    bx0, by1 = 366, ly - 4
    c.setLineWidth(1)
    c.rect(bx0, by1 - 74, 196, 74, stroke=1, fill=0)
    sy = by1 - 18
    sh.text(bx0 + 8, sy, "ACCOUNT SUMMARY", "Times-Bold", 8)
    sy -= 16
    for label, val, key, bold in (
            ("Total charges", totals["total_charges"], "total_charges", False),
            ("Self-pay discount", -totals["total_adjustments"], "total_adjustments", False),
            ("Amount due", totals["patient_balance"], "patient_balance", True)):
        f = "Times-Bold" if bold else "Times-Roman"
        sh.text(bx0 + 8, sy, label, f, 9)
        sh.text(bx0 + 188, sy, money(val), f, 9, align="right", key=key)
        sy -= 14

    sh.text(50, by1 - 18, "Self-pay price reflects the discount published in this", "Times-Italic", 8)
    sh.text(50, by1 - 29, "hospital's standard charges file.", "Times-Italic", 8)
    sh.text(50, by1 - 47, f"Payment due by {b['due_date']}.", "Times-Roman", 8.5, key="due_date")
    sh.text(50, by1 - 60, "Pay online at the hospital's billing portal.", "Times-Roman", 8.5)
    notice(sh, 50, min(by1 - 96, 300), 512,
           ["NOTICES", "An itemized statement listing every charge is available at no charge.",
            "Financial assistance may be available; ask Patient Financial Services.",
            "Keep this statement for your records."], family="Times")


def draw_insured(sh: Sheet, b: dict, hosp: dict, lines: list[dict], totals: dict) -> None:
    c = sh.c
    sh.banner(762, 34)
    sh.text(54, 726, hosp["name"], "Helvetica-Bold", 15, key="hospital_name")
    sh.text(54, 712, NR_ADDR, "Helvetica", 7.8, key="hospital_address")
    sh.text(558, 726, "PATIENT STATEMENT - INSURED", "Helvetica-Bold", 12, align="right")
    sh.text(558, 712, "After insurance processing", "Helvetica", 8, align="right")
    c.setLineWidth(0.8)
    c.line(54, 700, 558, 700)

    y = 684
    sh.text(54, y, "Patient", "Helvetica-Bold", 8)
    sh.text(54, y - 12, b["patient"], "Helvetica", 9.5, key="patient_name")
    sh.text(54, y - 24, b["patient_address"], "Helvetica", 8.2)
    sh.text(310, y, "Account number", "Helvetica-Bold", 8)
    sh.text(310, y - 12, b["account"], "Helvetica", 9.5, key="account_number")
    sh.text(440, y, "Statement date", "Helvetica-Bold", 8)
    sh.text(440, y - 12, b["statement_date"], "Helvetica", 9.5, key="statement_date")
    sh.text(310, y - 30, "Date of service", "Helvetica-Bold", 8)
    sh.text(310, y - 42, b["service_date"], "Helvetica", 9.5, key="service_date_start")
    sh.text(440, y - 30, "Health plan", "Helvetica-Bold", 8)
    sh.text(440, y - 42, b["payer"], "Helvetica", 8.5, key="payer")
    sh.text(54, y - 42, f"Visit type: {b['visit_type']}", "Helvetica", 8.5)

    cols = [(58, "Date", "left"), (116, "Code", "left"), (162, "Description", "left"),
            (400, "Billed", "right"), (452, "Allowed", "right"),
            (508, "Plan paid", "right"), (556, "You owe", "right")]

    def header_row(ty: float):
        c.setFillColorRGB(0.90, 0.90, 0.90)
        c.rect(54, ty - 4, 502, 15, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        for x, label, align in cols:
            sh.text(x, ty, label, "Helvetica-Bold", 8, align=align)

    def body_row(ln: dict, ry: float):
        size = sh.fit(ln["description"], "Helvetica", 9, 230)
        sh.text(58, ry, ln["date"], "Helvetica", 8.5)
        sh.text(116, ry, ln["code"], "Helvetica", 9)
        sh.text(162, ry, ln["description"], "Helvetica", size)
        sh.text(400, ry, money(ln["billed"]), "Helvetica", 9, align="right")
        sh.text(452, ry, money(ln["allowed"]), "Helvetica", 9, align="right")
        sh.text(508, ry, money(ln["plan_paid"]), "Helvetica", 9, align="right")
        sh.text(556, ry, money(ln["you_owe"]), "Helvetica", 9, align="right")
        sh.row_box(ln["line_no"], 54, ry, 558, 9)
        c.setStrokeColorRGB(0.82, 0.82, 0.82)
        c.setLineWidth(0.3)
        c.line(54, ry - 5, 558, ry - 5)

    y = 606
    sh.text(54, y, "FACILITY SERVICES - Norman Regional Health System (in-network facility)",
            "Helvetica-Bold", 9)
    y -= 16
    header_row(y)
    y -= 19
    for ln in [l for l in lines if l["section"] == "facility"]:
        body_row(ln, y)
        y -= 17

    y -= 12
    sh.text(54, y, f"PROFESSIONAL SERVICES - {b['professional_provider']}", "Helvetica-Bold", 9)
    y -= 13
    sh.text(54, y, b["network_note"], "Helvetica-Bold", 8.5, key="network_note")
    y -= 16
    header_row(y)
    y -= 19
    for ln in [l for l in lines if l["section"] == "professional"]:
        body_row(ln, y)
        y -= 17

    y -= 10
    c.setLineWidth(0.8)
    c.line(300, y + 10, 558, y + 10)
    rows = [("Total billed", totals["total_charges"], "total_charges", False),
            ("Plan discounts (contractual)", totals["contractual"], "contractual", False),
            ("Plan paid", totals["plan_paid"], "plan_paid", False),
            ("Patient cost share (in-network)", totals["cost_share"], "cost_share", False),
            ("Out-of-network balance billed", totals["balance_billed"], "balance_billed", False),
            ("Amount you owe", totals["patient_balance"], "patient_balance", True)]
    for label, val, key, bold in rows:
        f = "Helvetica-Bold" if bold else "Helvetica"
        sh.text(304, y, label, f, 9)
        sh.text(556, y, money(val), f, 9, align="right", key=key)
        y -= 14

    sh.text(54, y + 4, "Allowed, plan paid and cost-share amounts are fictional plan terms.",
            "Helvetica-Oblique", 8)
    sh.text(54, y - 9, f"Payment due by {b['due_date']}.", "Helvetica", 8.5, key="due_date")
    notice(sh, 54, 200, 504,
           ["MESSAGES", "This statement reflects amounts your health plan has processed.",
            "If you believe a provider was out of network, contact your plan before paying.",
            "Financial assistance and payment plans are available on request."])
    c.rect(54, 52, 504, 62, stroke=1, fill=0)
    sh.text(64, 98, "PAYMENT STUB - return with payment", "Helvetica-Bold", 8.5)
    sh.text(64, 84, f"Account {b['account']}", "Helvetica", 8.5)
    sh.text(64, 72, "Pay online at the hospital's billing portal", "Helvetica", 8.5)
    sh.text(548, 84, "Amount you owe", "Helvetica-Bold", 9, align="right")
    sh.text(548, 68, money(totals["patient_balance"]), "Helvetica-Bold", 12, align="right")


# ---------------------------------------------------------------- photo
def _offset(img: Image.Image, dx: int, dy: int) -> Image.Image:
    out = Image.new(img.mode, img.size, 0)
    out.paste(img, (dx, dy))
    return out


def _coeffs(dest: list[tuple[float, float]], src: list[tuple[float, float]]) -> tuple:
    """Perspective coefficients mapping output quad `dest` back to input quad `src`."""
    m = []
    for (dx, dy), (sx, sy) in zip(dest, src):
        m.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        m.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    a = np.array(m, dtype=np.float64)
    b = np.array(src, dtype=np.float64).reshape(8)
    return tuple(np.linalg.solve(a, b))


def make_photo(png: Path, out: Path, seed: int) -> None:
    """A phone snapshot of the printed page: tilt, mild perspective, desk, shadow,
    warm cast, focus softness and sensor noise, with the text left legible."""
    rng = np.random.default_rng(seed)
    page = Image.open(png).convert("RGB")
    scale = 1500 / page.width
    page = page.resize((int(page.width * scale), int(page.height * scale)), Image.LANCZOS)
    pw, ph = page.size
    W, H = int(pw * 1.16), int(ph * 1.10)

    # desk: warm flat colour with a soft diagonal light gradient and grain
    base = np.array([146.0, 126.0, 104.0])
    gx = np.linspace(-1, 1, W)[None, :]
    gy = np.linspace(-1, 1, H)[:, None]
    shade = 1.0 + 0.10 * (gx * 0.6 + gy * 0.4)
    desk = base[None, None, :] * shade[:, :, None]
    desk += rng.normal(0, 3.5, (H, W, 1))
    bg = Image.fromarray(np.clip(desk, 0, 255).astype(np.uint8), "RGB")

    angle = float(rng.uniform(1.5, 3.0)) * (1.0 if seed % 2 else -1.0)
    th = np.radians(angle)
    taper = float(rng.uniform(0.975, 0.99))   # top edge slightly narrower: mild perspective
    lift = float(rng.uniform(0.004, 0.012))
    corners = [(-pw / 2, -ph / 2), (pw / 2, -ph / 2), (pw / 2, ph / 2), (-pw / 2, ph / 2)]
    dest = []
    for i, (x, y) in enumerate(corners):
        if i < 2:                      # top corners
            x, y = x * taper, y * (1 - lift)
        xr = x * np.cos(th) - y * np.sin(th) + W / 2
        yr = x * np.sin(th) + y * np.cos(th) + H / 2
        dest.append((xr, yr))
    src = [(0, 0), (pw, 0), (pw, ph), (0, ph)]
    co = _coeffs(dest, src)

    rgba = page.convert("RGBA")
    warped = rgba.transform((W, H), Image.PERSPECTIVE, co, resample=Image.BICUBIC,
                            fillcolor=(0, 0, 0, 0))
    alpha = warped.getchannel("A")

    # soft contact shadow under the sheet
    sh = alpha.filter(ImageFilter.GaussianBlur(14))
    sh = _offset(sh, 10, 14)
    shn = np.asarray(sh, dtype=np.float32) / 255.0
    bgn = np.asarray(bg, dtype=np.float32) * (1.0 - 0.40 * shn)[:, :, None]
    bg = Image.fromarray(np.clip(bgn, 0, 255).astype(np.uint8), "RGB")

    bg.paste(warped, (0, 0), alpha)

    a = np.asarray(bg, dtype=np.float32)
    a *= np.array([1.045, 1.0, 0.945])                       # warm indoor cast
    a *= (1.0 + 0.05 * np.linspace(-1, 1, W))[None, :, None]  # uneven lighting
    a += rng.normal(0, 2.2, a.shape)                          # sensor noise
    photo = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")
    photo = photo.filter(ImageFilter.GaussianBlur(0.55))      # slight focus softness
    photo.save(out, "JPEG", quality=88, optimize=True)


# ---------------------------------------------------------------- build
def prepare(b: dict, H: Hospitals) -> dict:
    """Resolve one spec table against the fixtures; returns rows, totals and header fields."""
    n = int(b["id"].split("_")[1])
    hosp = H.by_id[b["hospital_id"]]
    out = dict(b)
    out["account"] = f"FB-2026-{n:06d}"
    out["statement_date"] = add_days(b["service_date"], 21)
    out["due_date"] = add_days(out["statement_date"], 30)
    out["hospital_address"] = NR_ADDR if b["hospital_id"] == "norman_regional" else OU_ADDR
    out["hospital_name"] = hosp["name"]

    rows: list[dict] = []
    if b["layout"] == "insured":
        for i, (code, sl, desc, billed, allowed, paid, owe, section) in enumerate(b["insured_lines"], 1):
            if sl is not None:
                H.checked_row(b["hospital_id"], code, sl, billed, None)
            rows.append(dict(line_no=i, code=code, source_line=sl, description=desc, units=1,
                             date=b["service_date"], billed=billed, allowed=allowed,
                             plan_paid=paid, you_owe=owe, section=section,
                             charge=billed, patient_price=owe))
        totals = dict(
            total_charges=round(sum(r["billed"] for r in rows), 2),
            total_adjustments=round(sum(r["billed"] - r["you_owe"] for r in rows), 2),
            contractual=round(sum(r["billed"] - r["allowed"] for r in rows if r["section"] == "facility"), 2),
            plan_paid=round(sum(r["plan_paid"] for r in rows), 2),
            cost_share=round(sum(r["you_owe"] for r in rows if r["section"] == "facility"), 2),
            balance_billed=round(sum(r["you_owe"] for r in rows if r["section"] == "professional"), 2),
            patient_balance=round(sum(r["you_owe"] for r in rows), 2))
    else:
        for i, spec_line in enumerate(b["lines"], 1):
            code, sl, desc, charge, price = spec_line[:5]
            # bill 2 line 5 prints gross in the "your price" column on purpose, so the row's
            # expected cash price is given explicitly; everywhere else it is the printed price.
            cash = spec_line[5] if len(spec_line) > 5 else price
            H.checked_row(b["hospital_id"], code, sl, charge, cash)
            rows.append(dict(line_no=i, code=code, source_line=sl, description=desc, units=1,
                             date=b["service_date"], charge=charge, patient_price=price))
        totals = dict(
            total_charges=round(sum(r["charge"] for r in rows), 2),
            total_adjustments=round(sum(r["charge"] - r["patient_price"] for r in rows), 2),
            patient_balance=round(sum(r["patient_price"] for r in rows), 2))
    out["rows"], out["totals"], out["hosp"] = rows, totals, hosp
    return out


def notes_for(b: dict) -> list[str]:
    n = [f"Patient '{b['patient']}', account {b['account']}, patient address, statement/service "
         f"dates and the billed line-up are fictional.",
         "Every charge and self-pay price is a verbatim row of the hospital's published standard "
         "charges file; see the evidence source_url and source_line.",
         "The billing error on this statement is planted for the demo; it is not a real hospital error."]
    if b["layout"] == "insured":
        n.append("Health plan, professional provider, and the allowed / plan paid / cost-share "
                 "amounts are fictional plan terms; no real payer contract is represented.")
    if b.get("clinical_note"):
        n.append("The visit summary text is fictional and contains no real patient information.")
    if not b["planted"]:
        n.append("This statement is clean: no error is planted.")
    return n


def build_one(b: dict, H: Hospitals) -> dict:
    pdf = BILLS_DIR / f"{b['id']}.pdf"
    png = BILLS_DIR / f"{b['id']}_page.png"
    jpg = BILLS_DIR / f"{b['id']}_photo.jpg"

    sh = Sheet(pdf)
    draw = {"norman": draw_norman, "ou": draw_ou, "insured": draw_insured}[b["layout"]]
    draw(sh, b, b["hosp"], b["rows"], b["totals"])
    sh.save()

    import fitz
    doc = fitz.open(pdf)
    doc[0].get_pixmap(dpi=DPI).save(png)
    doc.close()
    make_photo(png, jpg, SEED + int(b["id"].split("_")[1]))

    lines = []
    for r in b["rows"]:
        lines.append(BillLine(
            line_no=r["line_no"], date_of_service=r["date"], code=r["code"],
            code_type=code_type(r["code"]), description=r["description"], units=r["units"],
            charge=r["charge"], patient_price=r["patient_price"],
            provider=b.get("professional_provider") if r.get("section") == "professional" else None,
            network_note=(b["network_note"] if r.get("section") == "professional"
                          else ("In-network facility" if r.get("section") == "facility" else None)),
            crop=sh.line_crops[r["line_no"]]))

    bill = Bill(
        hospital_name=b["hospital_name"], hospital_address=b["hospital_address"],
        patient_name=b["patient"], account_number=b["account"],
        statement_date=b["statement_date"], service_date_start=b["service_date"],
        service_date_end=b["service_date"], payer=b["payer"], visit_type=b["visit_type"],
        clinical_note=b.get("clinical_note"), lines=lines,
        total_charges=b["totals"]["total_charges"],
        total_adjustments=b["totals"]["total_adjustments"],
        patient_balance=b["totals"]["patient_balance"],
        fictional_label_present=True, crops=sh.crops)

    planted = [Finding(
        kind=p["kind"], line_nos=p["line_nos"], summary=p["summary"],
        evidence=[H.file_row(b["hospital_id"], c, sl) for c, sl in p["evidence"]],
        amount_at_stake=p["amount_at_stake"], basis=p["basis"],
        needs_user_judgment=p.get("needs_user_judgment", False),
        regulation=p.get("regulation")) for p in b["planted"]]

    return {"id": b["id"], "hospital_id": b["hospital_id"],
            "files": {"pdf": f"gallery/bills/{pdf.name}", "page": f"gallery/bills/{png.name}",
                      "photo": f"gallery/bills/{jpg.name}"},
            "bill": bill.model_dump(), "planted": [f.model_dump() for f in planted],
            "clean": not planted, "notes": notes_for(b)}


def build_msn() -> Path:
    import fitz
    out = BILLS_DIR / "sample_msn_part_b_page1.png"
    doc = fitz.open(DATA / "samples" / "cms_sample_part_b_msn.pdf")
    doc[0].get_pixmap(dpi=DPI).save(out)
    doc.close()
    return out


def build_all() -> None:
    BILLS_DIR.mkdir(parents=True, exist_ok=True)
    H = Hospitals()
    entries = [build_one(prepare(b, H), H) for b in BILLS]
    (GALLERY / "truth.json").write_text(json.dumps(entries, indent=1), encoding="utf-8")
    msn = build_msn()
    print(f"wrote {len(entries)} bills to {BILLS_DIR}, {GALLERY / 'truth.json'}, {msn.name}")


# ---------------------------------------------------------------- check
def check() -> int:
    """Re-derive every bill from the spec tables + fixtures and compare to truth.json."""
    H = Hospitals()
    truth = json.loads((GALLERY / "truth.json").read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in truth}
    bad: list[str] = []
    rows_out = []

    for spec in BILLS:
        b = prepare(spec, H)          # re-asserts every fixture price; exits on mismatch
        e = by_id.get(b["id"])
        if e is None:
            bad.append(f"{b['id']}: missing from truth.json")
            continue
        for f in e["files"].values():
            if not (ROOT / f).exists():
                bad.append(f"{b['id']}: missing file {f}")
        lines = e["bill"]["lines"]
        if len(lines) != len(b["rows"]):
            bad.append(f"{b['id']}: {len(lines)} lines in truth.json, {len(b['rows'])} in spec")
        for want, got in zip(b["rows"], lines):
            for k, v in (("code", want["code"]), ("charge", want["charge"]),
                         ("patient_price", want["patient_price"]),
                         ("description", want["description"])):
                if got[k] != v:
                    bad.append(f"{b['id']} line {want['line_no']}: {k} {got[k]!r} != {v!r}")
            if got["crop"] is None:
                bad.append(f"{b['id']} line {want['line_no']}: no crop")
        for k in ("total_charges", "total_adjustments", "patient_balance"):
            if abs((e["bill"][k] or 0) - b["totals"][k]) > 0.005:
                bad.append(f"{b['id']}: {k} {e['bill'][k]} != {b['totals'][k]}")
        kinds_spec = [p["kind"] for p in b["planted"]]
        kinds_got = [p["kind"] for p in e["planted"]]
        if kinds_spec != kinds_got:
            bad.append(f"{b['id']}: planted {kinds_got} != spec {kinds_spec}")
        if e["clean"] != (not kinds_spec):
            bad.append(f"{b['id']}: clean flag {e['clean']} inconsistent with {kinds_spec}")
        # every FileRow must still match the fixture it cites
        for p_spec, p_got in zip(b["planted"], e["planted"]):
            if abs((p_got["amount_at_stake"] or 0) - p_spec["amount_at_stake"]) > 0.005:
                bad.append(f"{b['id']}: amount_at_stake {p_got['amount_at_stake']} "
                           f"!= {p_spec['amount_at_stake']}")
            for ev in p_got["evidence"]:
                it = H.row(ev["hospital_id"], ev["code"], ev["source_line"])
                if (it["description"] != ev["description"] or it.get("gross") != ev["gross"]
                        or it.get("discounted_cash") != ev["discounted_cash"]):
                    bad.append(f"{b['id']}: evidence {ev['code']}@{ev['source_line']} "
                               f"does not match the fixture row")
        rows_out.append((b["id"], b["hospital_id"], len(lines), ",".join(kinds_got) or "-",
                         b["totals"]["total_charges"], b["totals"]["patient_balance"]))

    if not (BILLS_DIR / "sample_msn_part_b_page1.png").exists():
        bad.append("missing gallery/bills/sample_msn_part_b_page1.png")

    hdr = ("bill", "hospital", "lines", "planted", "total charges", "patient owes")
    w = [max(len(str(r[i])) for r in rows_out + [hdr]) for i in range(6)]
    print("  ".join(str(h).ljust(w[i]) for i, h in enumerate(hdr)))
    print("  ".join("-" * w[i] for i in range(6)))
    for r in rows_out:
        cells = [str(r[0]).ljust(w[0]), str(r[1]).ljust(w[1]), str(r[2]).rjust(w[2]),
                 str(r[3]).ljust(w[3]), money(r[4]).rjust(w[4]), money(r[5]).rjust(w[5])]
        print("  ".join(cells))
    if bad:
        print("\nFAIL:")
        for m in bad:
            print("  " + m)
        return 1
    print(f"\nOK: {len(rows_out)} bills verified against the fixtures.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="fairbill.gallery_gen")
    ap.add_argument("--check", action="store_true", help="verify truth.json, do not rebuild")
    ns = ap.parse_args(argv)
    if ns.check:
        return check()
    build_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
