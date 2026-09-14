# Reader ablation: deskew on or off

Measured 2026-09-13 during the reader build, on the six gallery bills rendered as simulated phone photos (2 to 4 degrees of rotation, desk background) and scored with the reader evaluator in `evals/evaluators.py` (every line's code and amount must match `gallery/truth.json`).

| Reader | Bills read correctly | Misses |
|---|---|---|
| Two-pass reader, no deskew | 3 of 6 | bills 1, 2, 6 (the Norman layout) |
| Two-pass reader with OpenCV deskew (`_deskew` in `src/fairbill/reader.py`) | 6 of 6 | none |

Why the misses: on the wide Norman table, a 2 to 4 degree tilt across the roughly 1200 px from the Description column to the Your price column shifts the right-hand money columns by about one row height, so the model attached the wrong amount to the wrong line. Deskew finds the page quadrilateral and perspective-warps it flat before the model sees it. It changes geometry only, invents no pixel content, and no-ops when no page quad is found.

The current bench of record (`evals/BENCH.md`, 2026-09-14) runs with deskew on and reports reader 6 of 6. The no-deskew number is the reader build measurement of 2026-09-13; there is no runtime switch to turn deskew off, so reproducing it means calling the reader with `_deskew` bypassed.
