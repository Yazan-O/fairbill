"""Fairbill eval bench. PYTHONPATH=src python evals/run_bench.py

Two suites over the same six gallery bills:
  truth  the audit runs on the bill exactly as printed in gallery/truth.json
  e2e    the reader reads the phone photo first, then the audit runs on what it read
Writes evals/report.json and evals/BENCH.md.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # model text is UTF-8; the Windows console default is not
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strands_evals import Experiment  # noqa: E402
from strands_evals import EvaluationReport  # noqa: E402
from strands_evals.types.evaluation import NOT_APPLICABLE  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)  # strands_evals raises the root logger on import

from fairbill import audit, config  # noqa: E402
from fairbill.schema import Bill  # noqa: E402

import evaluators as ev  # noqa: E402
from cases import build_cases, truth_for  # noqa: E402

REPORT_JSON = ROOT / "evals" / "report.json"
BENCH_MD = ROOT / "evals" / "BENCH.md"

RECORDS: dict[str, dict] = {}


def _letters_available():
    """The judge only runs when both halves of the letter path import."""
    try:
        from fairbill.decisions import build_card
        from fairbill.letters import draft_letter
        return build_card, draft_letter
    except Exception:  # noqa: BLE001
        return None


LETTERS = _letters_available()


def _findings_text(result) -> str:
    out = []
    for f in result.findings:
        cited = "; ".join(f"{e.code} posted gross {e.gross} cash {e.discounted_cash} (file line {e.source_line})"
                          for e in f.evidence)
        out.append(f"- {f.kind} on lines {f.line_nos}: {f.summary} | at stake {f.amount_at_stake} "
                   f"({f.basis}) | {f.regulation or 'no regulation'} | evidence: {cited or 'none'}")
    return "\n".join(out) or "(no findings)"


def _draft(bill: Bill, result, bill_id: str):
    """The default letter for the first real option, or a reason there is none."""
    if LETTERS is None:
        return None, "skipped: fairbill.letters.draft_letter not importable yet"
    build_card, draft_letter = LETTERS
    card = build_card(bill, result, bill_id, dt.date.today())
    if card.default_option == "drop":
        return None, "clean bill: the decision card's default is to send nothing"
    letter = draft_letter(bill, result, card, card.default_option)
    return letter.model_dump(), None


def task(case) -> dict:
    """Sync on purpose: audit() calls asyncio.run internally, so it must not run inside a loop."""
    bill_id = str(case.input)
    entry = truth_for(case.name)
    suite = (case.metadata or {}).get("suite", "truth")
    rec = {"bill_id": bill_id, "suite": suite}
    t0 = time.time()

    out: dict = {}
    if suite == "e2e":
        from fairbill import reader
        bill, meta = reader.read_bill_with_meta(ROOT / entry["files"]["photo"])
        diffs = ev.reader_diffs(bill_id, bill)
        out["read_diffs"] = diffs
        rec["read_exact"] = not diffs
        rec["read_seconds"] = round(float(meta.get("seconds") or 0.0), 1)
    else:
        bill = Bill(**entry["bill"])

    result = audit.audit(bill, bill_id)
    out["audit"] = result.model_dump()
    out["findings_text"] = _findings_text(result)
    letter, note = _draft(bill, result, bill_id)
    out["letter"], out["letter_note"] = letter, note

    rec["seconds"] = round(time.time() - t0, 1)
    rec["planted"] = len(entry.get("planted", []))
    rec["found"] = len(result.findings)
    RECORDS[case.name] = rec
    return {"output": out, "input": bill_id}


def run_suite(suite: str, evaluators) -> EvaluationReport:
    cases = build_cases(suite)
    print(f"# running suite '{suite}' on {len(cases)} bills ...", flush=True)
    return Experiment(cases=cases, evaluators=evaluators).run_evaluations(task)


def _by_case(report: EvaluationReport) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for i, (row, score) in enumerate(zip(report.cases, report.scores)):
        outs = report.detailed_results[i] if i < len(report.detailed_results) else []
        if outs and all(o.label == NOT_APPLICABLE and o.test_pass for o in outs):
            continue  # declined to judge: no verdict to average or print
        scores.setdefault(str(row.get("name")), {})[row["evaluator"]] = score
    return scores


def table(per: dict) -> str:
    head = ("| bill | suite | planted | found | matched | false flags | letter score | reader exact | seconds |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    rows = []
    for name in sorted(RECORDS):
        r, s = RECORDS[name], per.get(name, {})
        planted = r["planted"]
        matched = int(round(s.get("FindingsMatch", 0.0) * planted))
        letter = s.get("LetterQuality")
        letter_cell = "-" if (LETTERS is None or letter is None) else f"{letter:.2f}"
        reader_cell = "-" if r["suite"] != "e2e" else ("yes" if r.get("read_exact") else "no")
        extra = r["found"] - matched
        rows.append(f"| {r['bill_id']} | {r['suite']} | {planted} | {r['found']} | {matched}/{planted} | "
                    f"{extra} | {letter_cell} | {reader_cell} | {r['seconds']} |")
    return head + "\n" + "\n".join(rows)


def main() -> int:
    config.load_env()
    if LETTERS is None:
        print("# letters.draft_letter not importable: letter column skipped (judge evaluator stays wired)")
    evaluators = [ev.FindingsMatch(), ev.FalseFlags(), ev.EvidenceCited(), ev.ReaderExact(), ev.LetterQuality()]

    t0 = time.time()
    reports = [run_suite("truth", evaluators), run_suite("e2e", evaluators)]
    merged = EvaluationReport.flatten(reports)
    elapsed = round(time.time() - t0, 1)

    per = _by_case(merged)
    planted_total = sum(r["planted"] for r in RECORDS.values())
    matched_total = sum(int(round(per.get(n, {}).get("FindingsMatch", 0.0) * RECORDS[n]["planted"]))
                        for n in RECORDS)
    false_rows = [n for n in RECORDS if per.get(n, {}).get("FalseFlags") != 1.0]
    e2e = [n for n in RECORDS if RECORDS[n]["suite"] == "e2e"]
    reader_ok = sum(1 for n in e2e if RECORDS[n].get("read_exact"))
    letter_scores = [per[n]["LetterQuality"] for n in per if "LetterQuality" in per[n]]

    md = table(per)
    suite_bits = []
    for suite in ("truth", "e2e"):
        names = [n for n in RECORDS if RECORDS[n]["suite"] == suite]
        suite_bits.append(
            f"{sum(int(round(per.get(n, {}).get('FindingsMatch', 0.0) * RECORDS[n]['planted'])) for n in names)}"
            f"/{sum(RECORDS[n]['planted'] for n in names)} {suite}")
    summary = (f"\n**{' and '.join(suite_bits)} planted findings matched "
               f"({matched_total}/{planted_total} overall), {len(false_rows)} row(s) with a "
               f"false flag, reader {reader_ok}/{len(e2e)} exact"
               + (f", letter score mean {sum(letter_scores) / len(letter_scores):.2f}" if letter_scores else
                  ", letter column skipped")
               + f", {elapsed}s total.**\n")
    BENCH_MD.write_text(
        "# Fairbill bench\n\nGround truth is `gallery/truth.json`. Suite `truth` audits the bill as printed; "
        "suite `e2e` reads the phone photo first, then audits what it read.\n\n" + md + "\n" + summary,
        encoding="utf-8")

    REPORT_JSON.write_text(json.dumps({
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "overall_score": merged.overall_score,
        "elapsed_seconds": elapsed,
        "letters_available": LETTERS is not None,
        "totals": {"planted": planted_total, "matched": matched_total,
                   "false_flag_rows": len(false_rows),
                   "reader_exact": reader_ok, "reader_rows": len(e2e)},
        "rows": [{"case": row.get("name"), "evaluator": row["evaluator"],
                  "score": s, "test_pass": p, "reason": reason}
                 for row, s, p, reason in zip(merged.cases, merged.scores,
                                              merged.test_passes, merged.reasons)],
        "bills": RECORDS,
    }, indent=2, default=str), encoding="utf-8")

    print("\n" + md + summary)
    print(f"wrote evals/BENCH.md and evals/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
