"""The guard: Fairbill never moves money, whatever anyone types.

A Strands intervention sits in front of every tool call. A tool whose name looks
like a payment, or whose arguments carry a card, bank or routing number, is
denied before it runs; the denial goes into the ledger (actor guard) and back to
the model as the tool result, so the refusal is visible in the trace instead of
being silently swallowed. on_error='deny' means a broken guard fails closed.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from strands import Agent, tool
from strands.interventions import Deny, InterventionHandler, Proceed

from fairbill import ledger
from fairbill.config import down_reason, live_ladder, make_model, mark_down, model_name, retry_strategy
from fairbill.tools import code_lookup

# Tool names are split on _ - whitespace and camelCase, then matched whole-token, so
# design_review and assign_case pass while email_billing and submitPayment do not.
NAME_TOKENS = frozenset({
    "pay", "payment", "charge", "transfer", "submit", "sign", "send", "email", "mail",
    "fax", "wire", "ach", "autopay", "remit", "purchase", "checkout", "invoice_pay",
})
_TOKEN_SPLIT = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# A card number is 13-19 digits, contiguous or in space/dash separated groups of four.
# Digits alone with any separator would also swallow two ISO dates in a row, so the
# grouped form is spelled out instead.
CARD_NUMBER = re.compile(r"(?<!\d)\d{13,19}(?!\d)|(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)")
ARG_PATTERNS = [
    (CARD_NUMBER, "a card-number-shaped value"),
    (re.compile(r"routing", re.I), "a routing number"),
    (re.compile(r"cvv", re.I), "a card security code"),
    (re.compile(r"\biban\b", re.I), "a bank account (IBAN)"),
]
ACCOUNT_PAY = (re.compile(r"account_number", re.I), re.compile(r"pay", re.I))

# proof that the decoy never runs
pay_bill_calls = 0


@tool
def pay_bill(amount: float, method: str) -> str:
    """Pay a hospital bill. Fairbill never uses this; it exists so the guard can refuse it.

    Args:
        amount: Amount to pay in USD.
        method: How to pay, e.g. "card" or "bank".

    Returns:
        A string describing the payment that would have been made.
    """
    global pay_bill_calls
    pay_bill_calls += 1
    return f"paid {amount} by {method}"


def _walk(value: Any) -> list[str]:
    """Every key and string value in a nested tool input."""
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(_walk(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_walk(v))
    elif value is not None:
        out.append(str(value))
    return out


def name_tokens(tool_name: str) -> list[str]:
    """The tool name split into lowercase word pieces."""
    return [t.lower() for t in _TOKEN_SPLIT.split(tool_name or "") if t]


def name_blocked(tool_name: str) -> bool:
    return any(t in NAME_TOKENS for t in name_tokens(tool_name))


def screen(tool_name: str, tool_input: Any) -> Optional[str]:
    """The reason to deny this call, or None to let it through."""
    if name_blocked(tool_name):
        return (f"Fairbill does not move money. The tool '{tool_name}' would pay, sign or send "
                "on your behalf, so it is blocked. Use the letter and the deadline instead.")
    parts = _walk(tool_input)
    blob = " ".join(parts)
    for pat, what in ARG_PATTERNS:
        if pat.search(blob):
            return (f"Blocked: the call to '{tool_name}' carries {what}. Fairbill never handles "
                    "payment or bank details.")
    acct, pay = ACCOUNT_PAY
    if acct.search(blob) and pay.search(blob):
        return (f"Blocked: the call to '{tool_name}' pairs an account number with a payment "
                "instruction. Fairbill never handles payment details.")
    return None


class MoneyGuard(InterventionHandler):
    """Denies any tool call that would move money. Fails closed."""

    name = "fairbill_money_guard"
    on_error = "deny"

    def __init__(self, session_id: str = "guard", bill_id: str | None = None):
        self.session_id = session_id
        self.bill_id = bill_id
        self.calls: list[dict] = []

    def before_tool_call(self, event):
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        reason = screen(name, tool_use.get("input"))
        if reason:
            self.calls.append({"name": name, "status": "denied", "reason": reason})
            ledger.record(self.session_id, "guard_denial", reason, bill_id=self.bill_id,
                          evidence=[f"tool {name}"], undo=None, actor="guard")
            return Deny(reason=reason)
        return Proceed()

    def after_tool_call(self, event):
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        if not any(c["name"] == name and c["status"] == "denied" for c in self.calls):
            self.calls.append({"name": name, "status": "ok", "reason": ""})
        return Proceed()


# ---- PII redaction ---------------------------------------------------------

def redact(text: str, patient_name: str | None = None, account_number: str | None = None) -> str:
    """Mask the patient's name, the account number and any card-length digit run."""
    out = text or ""
    if account_number:
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(account_number)}(?![A-Za-z0-9])",
                     "[account]", out, flags=re.I)
    out = re.sub(r"\bFB-\d{4}-\d{6}\b", "[account]", out)
    out = CARD_NUMBER.sub("[number]", out)
    if patient_name:
        out = re.sub(rf"\b{re.escape(patient_name)}\b", "[patient]", out, flags=re.I)
        for part in patient_name.split():
            if len(part) > 2:
                out = re.sub(rf"\b{re.escape(part)}\b", "[patient]", out, flags=re.I)
    return out


class RedactionHook(InterventionHandler):
    """Masks patient name and account number in every tool input and every tool result.

    The masking happens in place on the tool_use input (before the tool runs) and on the
    result content (after it runs), both of which the SDK's hook-event write guard allows.
    """

    name = "fairbill_redaction"
    # on_error is not vacuous: before_tool_call is a real cancel point, so a redactor that
    # throws cancels the call instead of letting unmasked PII reach the tool.
    on_error = "deny"

    def __init__(self, patient_name: str | None = None, account_number: str | None = None):
        self.patient_name = patient_name
        self.account_number = account_number
        self.seen: list[str] = []

    def scrub(self, text: str) -> str:
        out = redact(text, self.patient_name, self.account_number)
        # one entry per masked value, so the on-screen count is a count of values
        self.seen.extend(re.findall(r"\[(?:patient|account|number)\]", out)[
            len(re.findall(r"\[(?:patient|account|number)\]", text)):])
        return out

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self._scrub_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub_value(v) for v in value]
        return value

    def before_tool_call(self, event):
        tool_use = getattr(event, "tool_use", None)
        if isinstance(tool_use, dict) and isinstance(tool_use.get("input"), dict):
            inp = tool_use["input"]
            for k in [*inp]:
                inp[k] = self._scrub_value(inp[k])
        return Proceed()

    def after_tool_call(self, event):
        result = getattr(event, "result", None)
        if isinstance(result, dict):
            for block in result.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = self.scrub(block["text"])
        return Proceed()


# ---- the patient-facing chat ----------------------------------------------

# Per model; two models fit inside the proxy's stream window. Retry math: 3 attempts at
# 2 s then 4 s of backoff is 6 s of waiting plus at most three chat turns of inference
# (about 8 s each, measured 2026-09-14), roughly 30 s, so the whole primary ladder fits
# inside the 75 s budget and a single ThrottlingException is absorbed by a retry, not by Haiku.
CHAT_TIMEOUT_S = 75

SYSTEM_PROMPT = (
    "You help a patient understand this bill. You never move money. "
    "The refusal is enforced in code, not by you: when the patient asks to pay, call the "
    "pay_bill tool so the guard's refusal is on the record, then tell the patient what "
    "happened and point to the letter and the deadline. Never ask the patient for payment "
    "details: use the card's amount at stake and method \"card\" for that call. "
    "Answer in at most four sentences, plain language, from the decision card you are given.")


def _card_text(card) -> str:
    if card is None:
        return "No decision card was loaded for this bill."
    opts = "; ".join(f"{o.id}: {o.label} ({o.consequence})" for o in card.options)
    return (f"Decision card for {card.bill_id}\nSituation: {card.situation}\n"
            f"Options: {opts}\nDefault: {card.default_option}\n"
            f"Deadline: {card.deadline.isoformat()} ({card.deadline_reason})\n"
            f"Amount at stake: ${card.amount_at_stake:,.2f}\nEvidence: {card.evidence_url}")


async def chat_async(session_id: str, bill_id: str, text: str, card=None, bill=None) -> dict:
    """Returns {reply, tool_calls:[{name,status,reason}], redactions}.

    The redactor is built from the bill, which is where the patient name and the account
    number actually live; the guard runs first so it screens the unmasked input.
    """
    guard = MoneyGuard(session_id=session_id, bill_id=bill_id)
    redactor = RedactionHook(
        patient_name=getattr(bill, "patient_name", None),
        account_number=getattr(bill, "account_number", None))
    prompt = f"{_card_text(card)}\n\nPatient says: {text}"
    reply = ""
    notes: list[str] = []
    rungs = live_ladder()
    used = rungs[0]
    for mid in rungs:  # the same ladder as every other stage, each rung with a time budget
        if mid != rungs[0] and down_reason(mid):
            continue
        agent = Agent(model=make_model(model=mid), system_prompt=SYSTEM_PROMPT,
                      tools=[pay_bill, code_lookup], interventions=[guard, redactor],
                      callback_handler=None,
                      retry_strategy=retry_strategy())
        try:
            res = await asyncio.wait_for(agent.invoke_async(prompt), CHAT_TIMEOUT_S)
            reply = str(res)
            used = mid
            if mid != rungs[0]:  # a step down the ladder is never silent
                why = down_reason(rungs[0]) or "failed"
                notes.append(f"{model_name(rungs[0])} {why}; answered with {model_name(mid)}")
            break
        except Exception as exc:  # noqa: BLE001 - the guard's verdict still has to reach the caller
            reply = f"Fairbill could not answer just now ({type(exc).__name__})."
            notes.append(f"chat attempt on {model_name(mid)} failed: {type(exc).__name__}")
            mark_down(mid, exc)
    reply = redactor.scrub(reply.strip())
    return {"reply": reply, "tool_calls": guard.calls, "redactions": len(redactor.seen),
            "notes": notes, "model": used}


def chat(session_id: str, bill_id: str, text: str, card=None, bill=None) -> str:
    """Sync wrapper per the spec signature: returns the reply text.
    Use chat_async for the reply plus the tool-call trace."""
    return asyncio.run(chat_async(session_id, bill_id, text, card, bill))["reply"]
