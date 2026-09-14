"""Deterministic evaluators plus one LLM judge, as strands_evals Evaluator subclasses.

Each class checks one claim from web/SPEC.md section D. The Experiment runs the task once per
case and feeds the same result to every evaluator, so a bill is audited once, not once per check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from strands_evals.evaluators import Evaluator
from strands_evals.types import EvaluationData, EvaluationOutput
from strands_evals.types.evaluation import NOT_APPLICABLE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fairbill import reader  # noqa: E402

from cases import truth_for  # noqa: E402

# out_of_network is proved by the bill's own network wording, not by a price-file row, so
# truth.json plants it with empty evidence and the citation check exempts it.
NO_FILE_ROW_KINDS = {"out_of_network"}
JUDGE_PASS = 0.8


def _out(data: EvaluationData) -> dict:
    return data.actual_output if isinstance(data.actual_output, dict) else {}


def _found(data: EvaluationData) -> list[dict]:
    return (_out(data).get("audit") or {}).get("findings", [])


def _planted(data: EvaluationData) -> list[dict]:
    exp = data.expected_output or {}
    return exp.get("planted", []) if isinstance(exp, dict) else []


def _key(f: dict) -> tuple[str, tuple[int, ...]]:
    return (f["kind"], tuple(sorted(f.get("line_nos") or [])))


def _na(reason: str) -> list[EvaluationOutput]:
    return [EvaluationOutput(score=0.0, test_pass=True, label=NOT_APPLICABLE, reason=reason)]


def _one(score: float, passed: bool, reason: str) -> list[EvaluationOutput]:
    return [EvaluationOutput(score=score, test_pass=passed, reason=reason)]


class FindingsMatch(Evaluator):
    """Every planted finding is found with the same kind and the same bill lines."""

    def evaluate(self, evaluation_data: EvaluationData, **kwargs: Any) -> list[EvaluationOutput]:
        planted, found = _planted(evaluation_data), _found(evaluation_data)
        want = {_key(f) for f in planted}
        got = {_key(f) for f in found}
        if not want:
            ok = not found
            return _one(1.0 if ok else 0.0, ok,
                        "clean bill, no findings" if ok else f"clean bill but {len(found)} finding(s) raised")
        hit = want & got
        score = len(hit) / len(want)
        missed = sorted(f"{k}{list(n)}" for k, n in want - hit)
        return _one(score, score == 1.0,
                    f"matched {len(hit)}/{len(want)} planted" + (f"; missed {', '.join(missed)}" if missed else ""))


class FalseFlags(Evaluator):
    """No finding outside the planted set. One false flag scores 0."""

    def evaluate(self, evaluation_data: EvaluationData, **kwargs: Any) -> list[EvaluationOutput]:
        want = {_key(f) for f in _planted(evaluation_data)}
        extra = sorted(f"{k}{list(n)}" for k, n in {_key(f) for f in _found(evaluation_data)} - want)
        return _one(0.0 if extra else 1.0, not extra,
                    f"{len(extra)} false flag(s): {', '.join(extra)}" if extra else "0 false flags")


class EvidenceCited(Evaluator):
    """Every finding cites a price-file row with a source_line, except out_of_network."""

    def evaluate(self, evaluation_data: EvaluationData, **kwargs: Any) -> list[EvaluationOutput]:
        checked = [f for f in _found(evaluation_data) if f["kind"] not in NO_FILE_ROW_KINDS]
        if not checked:
            return _one(1.0, True, "no finding requires a file row")
        bad = [f["kind"] for f in checked
               if not any(isinstance(e.get("source_line"), int) for e in (f.get("evidence") or []))]
        score = (len(checked) - len(bad)) / len(checked)
        return _one(score, not bad,
                    f"{len(checked) - len(bad)}/{len(checked)} cite a file row"
                    + (f"; uncited: {', '.join(bad)}" if bad else ""))


class ReaderExact(Evaluator):
    """End-to-end rows only: the Bill read from the photo matches the printed bill field for field."""

    def evaluate(self, evaluation_data: EvaluationData, **kwargs: Any) -> list[EvaluationOutput]:
        out = _out(evaluation_data)
        if "read_diffs" not in out:
            return _na("truth-fed row, the reader did not run")
        diffs = out["read_diffs"]
        return _one(1.0 if not diffs else 0.0, not diffs,
                    "exact match" if not diffs
                    else "; ".join(f"line {d['line_no']} {d['field']}: expected {d['expected']}, got {d['got']}"
                                   for d in diffs[:4]))


class LetterQuality(Evaluator):
    """LLM judge on the drafted default letter (SPEC.md D rubric). Pass at 0.8."""

    RUBRIC = (
        "Score a patient's dispute letter from 0 to 1 on four things, each worth 0.25:\n"
        "1. It quotes the hospital's own price-file row (a code and the posted price) as evidence.\n"
        "2. It names the regulation or right it relies on.\n"
        "3. It invents no dollar amount: every amount appears in the audit findings or the bill below.\n"
        "4. Plain language a patient can read, under 350 words, no legal jargon.\n"
        "Return the score, test_pass=true only if the score is at least 0.8, and one sentence of reason."
    )

    def __init__(self, model=None, **kw):
        super().__init__(**kw)
        self._model = model

    def _judge(self):
        from strands import Agent

        from fairbill import config
        # callback_handler=None: the judge must not stream to the console (the bench prints the table).
        return Agent(model=self._model or config.make_model(temperature=0.0),
                     callback_handler=None,
                     system_prompt="You grade patient advocacy letters strictly against a rubric.")

    def evaluate(self, evaluation_data: EvaluationData, **kwargs: Any) -> list[EvaluationOutput]:
        out = _out(evaluation_data)
        letter = out.get("letter")
        if letter is None:
            return _na(out.get("letter_note") or "no letter drafted for this bill")
        prompt = (f"{self.RUBRIC}\n\n--- LETTER ---\n{letter['body']}\n\n"
                  f"Citations offered: {letter.get('citations')}\n\n"
                  f"--- AUDIT FINDINGS (the only true amounts) ---\n{out.get('findings_text', '')}\n")
        res = self._judge()(prompt, structured_output_model=EvaluationOutput)
        ev = res.structured_output
        score = max(0.0, min(1.0, float(ev.score)))
        # The 0.8 threshold is SPEC's, not the model's.
        return [EvaluationOutput(score=score, test_pass=score >= JUDGE_PASS, reason=ev.reason)]


def reader_diffs(bill_id: str, got) -> list[dict]:
    """Phase-3 comparison, imported so the bench and the reader report cannot drift apart."""
    entry = truth_for(f"{bill_id}_e2e")
    return reader._bench_one(entry, got)
