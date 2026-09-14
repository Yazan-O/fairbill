# Fairbill evals

Run: `PYTHONPATH=src python evals/run_bench.py` (real Bedrock calls, about 4 minutes).
The ground truth is `gallery/truth.json`: each gallery bill plus the findings planted in it.
Built on Strands Evals: `cases.py` makes one `Case` per bill, `evaluators.py` holds one `Evaluator` per check, `run_bench.py` runs two `Experiment`s and writes `report.json` and `BENCH.md`.
Suite `truth` audits the bill exactly as printed; suite `e2e` reads the phone photo first, then audits what the reader read.

Columns: **planted** findings in the bill, **found** by the audit, **matched** (same kind and the same bill lines), **false flags** (findings that were not planted), **letter score** (LLM judge, 0 to 1, passes at 0.8), **reader exact** (every field of the read bill matches the printed bill), **seconds** end to end for that row.
Evaluators: `FindingsMatch`, `FalseFlags`, `EvidenceCited` (every finding cites a price-file row with a source line, except `out_of_network`, which the bill's own network wording proves), `ReaderExact` (e2e rows only), `LetterQuality` (judge on the default letter: cites the file row and the regulation, invents no amount, plain language, under 350 words).
Rows that cannot be judged (no letter on the clean bill, no reader on a truth-fed row) are marked not-applicable and left out of the averages.
