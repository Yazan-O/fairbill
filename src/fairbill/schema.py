"""Shared data contracts for Fairbill. Every agent, tool, fixture, and eval speaks these types.

Bill: what the Reader extracts from a photo or PDF (every field carries the crop it came from).
Finding: what the audit Graph produces per problem. Truth (gallery/truth.json) is a Bill plus the
planted Findings, so the eval bench compares Finding lists directly.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

CodeType = Literal["CPT", "HCPCS", "RC", "NDC", "DRG", "unknown"]
AuditStatus = Literal["audited", "unsupported_hospital", "incomplete"]
FindingKind = Literal["duplicate", "above_cash", "unbundled", "upcoded", "out_of_network", "unmatched"]


class Crop(BaseModel):
    """Where on the page a value was read. Pixel box on the source image; page is 0-based."""
    page: int = 0
    x0: int
    y0: int
    x1: int
    y1: int


class BillLine(BaseModel):
    line_no: int = Field(description="1-based order on the statement")
    date_of_service: Optional[str] = Field(None, description="ISO date if printed")
    code: Optional[str] = Field(None, description="CPT/HCPCS/revenue code as printed, e.g. '85025'")
    code_type: CodeType = "unknown"
    modifier: Optional[str] = None
    description: str
    units: float = 1
    charge: float = Field(description="Gross charge printed for the line, USD")
    patient_price: Optional[float] = Field(None, description="Per-line self-pay or 'your price' if printed")
    provider: Optional[str] = Field(None, description="Rendering provider or group if the line names one")
    network_note: Optional[str] = Field(None, description="Any in/out-of-network wording next to the line")
    crop: Optional[Crop] = None


class Bill(BaseModel):
    hospital_name: str
    hospital_address: Optional[str] = None
    patient_name: str
    account_number: Optional[str] = None
    statement_date: Optional[str] = None
    service_date_start: Optional[str] = None
    service_date_end: Optional[str] = None
    payer: str = Field(description="Insurance plan name as printed, or 'Self-pay'")
    visit_type: Optional[str] = Field(None, description="e.g. Emergency, Outpatient lab, Outpatient procedure")
    clinical_note: Optional[str] = Field(None, description="Any visit summary or reason-for-visit text printed on the bill")
    lines: list[BillLine]
    total_charges: Optional[float] = None
    total_adjustments: Optional[float] = None
    patient_balance: Optional[float] = None
    fictional_label_present: bool = Field(False, description="True if the page says the patient is fictional")
    crops: dict[str, Crop] = Field(default_factory=dict, description="Crops for header fields, keyed by field name")


class FileRow(BaseModel):
    """One row of a hospital's standard-charge file, as evidence."""
    hospital_id: str
    code: str
    description: str
    setting: Optional[str] = None
    gross: Optional[float] = None
    discounted_cash: Optional[float] = None
    source_line: int = Field(description="1-based line in the raw CSV")
    source_url: str
    fetched_on: str


class Finding(BaseModel):
    kind: FindingKind
    line_nos: list[int] = Field(description="Bill lines involved")
    summary: str = Field(description="One sentence a patient can read")
    evidence: list[FileRow] = Field(default_factory=list)
    amount_at_stake: Optional[float] = Field(None, description="USD the patient may be over-billed, at the price basis the bill uses")
    basis: Literal["gross", "cash", "unknown"] = "unknown"
    needs_user_judgment: bool = False
    regulation: Optional[str] = Field(None, description="e.g. '45 CFR 149.410 (No Surprises Act)'")
    confidence: float = Field(1.0, ge=0, le=1)


class AuditResult(BaseModel):
    bill_id: str
    hospital_id: Optional[str]
    findings: list[Finding]
    clean: bool = Field(description="True when no finding survived validation")
    file_used: Optional[FileRow] = Field(None, description="Header-level pointer: hospital file, URL, fetch date")
    notes: list[str] = Field(default_factory=list)
    status: AuditStatus = Field(
        "audited",
        description="audited: every specialist ran against the hospital's own price file. "
        "unsupported_hospital: the bill's hospital is not in the registry, so nothing was checked. "
        "incomplete: at least one specialist failed, so the bill was only partly checked.",
    )


# ---- Phase 5: decisions, letters, ledger, fetch ----------------------------

OptionId = Literal["dispute", "itemized", "drop", "ask_provider", "pay_in_network"]


class Option(BaseModel):
    """One thing the patient can do, with what follows from it."""
    id: OptionId
    label: str
    consequence: str


class DecisionCard(BaseModel):
    """What the patient decides, in one screen: situation, options, deadline, evidence."""
    bill_id: str
    situation: str = Field(description="At most two sentences a patient can read")
    status: AuditStatus = Field(
        "incomplete",  # fail closed: a card that does not say it was audited was not
        description="Mirrors AuditResult.status: what the card is allowed to claim was checked.")
    options: list[Option] = Field(min_length=1, max_length=3)
    default_option: OptionId
    deadline: date
    deadline_reason: str
    evidence_url: str
    evidence_lines: list[int] = Field(default_factory=list, description="Price-file source lines")
    amount_at_stake: float = 0.0
    basis: Literal["gross", "cash", "unknown"] = "unknown"
    needs_user_judgment: bool = False
    clean: bool = False
    kind: Optional[FindingKind] = Field(None, description="Leading finding kind; None on a clean bill")


class Letter(BaseModel):
    """A letter the patient can print and mail. Body is plain text, no markup."""
    bill_id: str
    kind: Literal["dispute", "itemized_request", "nsa_complaint"]
    to_name: str
    to_address: str
    from_name: str
    re_line: str
    body: str
    citations: list[str] = Field(default_factory=list)
    footer: str = ""
    notes: list[str] = Field(default_factory=list,
                             description="Filled in by Fairbill, never by the model: leave empty.")


class LedgerEntry(BaseModel):
    """One append-only row: what happened, why, and how to take it back."""
    id: str
    ts: str = Field(description="ISO 8601 UTC")
    session_id: str
    bill_id: Optional[str] = None
    action: str
    why: str = ""
    evidence: list[str] = Field(default_factory=list)
    undo: Optional[str] = Field(None, description="Label of the undo action; None means the row cannot be undone")
    undone: bool = False
    final_after: Optional[str] = Field(None, description="ISO 8601 UTC; end of the veto window")
    actor: Literal["agent", "user", "guard"] = "agent"


class FetchReport(BaseModel):
    """What happened when Fairbill went to the hospital's own price-file URL."""
    hospital_id: str
    url: str
    status: Literal["live", "fixture", "live_partial"]
    bytes: int = 0
    total_bytes: Optional[int] = None
    seconds: float = 0.0
    sha256: Optional[str] = None
    matched_fixture: Optional[bool] = None
    note: str = ""
