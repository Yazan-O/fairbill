"""Kills three claims behind the phone finding "the same flag showed twice":
(a) the validator drops a claim two specialists both emit, (b) the reader never hands the
page two rows with the same line_no, (c) the model registry skips a capped rung.
Runs offline: bill_02 comes from gallery/truth.json and the price rows from the local
hospital index. Run: PYTHONPATH=src python -m pytest evals/test_source_dedupe.py -q
"""
import json
from pathlib import Path

from fairbill import config
from fairbill.audit import Claim, validate
from fairbill.reader import _check
from fairbill.schema import Bill

ROOT = Path(__file__).resolve().parents[1]


def _bill_02() -> Bill:
    item = next(x for x in json.loads((ROOT / "gallery/truth.json").read_text()) if x["id"] == "bill_02")
    return Bill.model_validate(item["bill"])


def test_validator_keeps_one_finding_when_two_specialists_emit_the_same_claim():
    bill = _bill_02()
    claim = {"kind": "above_cash", "line_nos": [5], "summary": "Chest X-ray charged above the cash price.",
             "evidence_codes": ["71046"]}
    claims = [("coding_specialist", Claim(**claim)), ("rights_specialist", Claim(**claim)),
              ("coding_specialist", Claim(**claim))]  # a re-delivered event looks like a third copy
    findings, _notes = validate(claims, bill, "norman_regional")
    assert [(f.kind, f.line_nos) for f in findings] == [("above_cash", [5])]


def test_validator_dedupes_on_lines_not_wording():
    bill = _bill_02()
    a = Claim(kind="above_cash", line_nos=[5], summary="one wording", evidence_codes=["71046"])
    b = Claim(kind="above_cash", line_nos=[5], summary="another wording entirely", evidence_codes=["71046"])
    findings, _ = validate([("coding_specialist", a), ("rights_specialist", b)], bill, "norman_regional")
    assert len(findings) == 1


def test_reader_renumbers_rows_that_share_a_line_no_without_dropping_any():
    bill = _bill_02()
    rows = [ln.model_copy() for ln in bill.lines]
    rows[3].line_no = 3  # the model numbered rows 3 and 4 the same
    out = _check(bill.model_copy(update={"lines": rows}))
    assert [ln.line_no for ln in out.lines] == [1, 2, 3, 4, 5]
    assert [ln.code for ln in out.lines] == [ln.code for ln in bill.lines]  # nothing dropped, order kept


def test_reader_leaves_good_numbering_alone():
    bill = _bill_02()
    out = _check(bill)
    assert [ln.line_no for ln in out.lines] == [1, 2, 3, 4, 5]


def test_registry_skips_a_rung_that_hit_its_daily_cap():
    config.clear_down()
    try:
        rungs = config.ladder()
        why = config.mark_down(rungs[0], RuntimeError(
            "ThrottlingException: Too many tokens per day, please wait before trying again"))
        assert why == "hit its daily token limit on Bedrock"
        assert config.live_ladder() == rungs[1:]
        assert config.live_ladder()  # never empty
    finally:
        config.clear_down()
