# Fairbill demo gallery
Six patient statements a judge can audit from a phone, plus one real public CMS sample.

**Real:** every charge and self-pay price on every line is a verbatim row of a real hospital's
published standard-charges file (Norman Regional Health System, OU Health - University of
Oklahoma Medical Center, both fetched 2026-09-13); `truth.json` cites the source URL, fetch date
and raw CSV line for each. `bills/sample_msn_part_b_page1.png` is page 1 of the real CMS
Medicare Summary Notice sample in `data/samples/`.

**Fictional:** patients, accounts, addresses, dates, bill 5's health plan and professional group
with its allowed / plan-paid / cost-share amounts, bill 4's visit summary, and every planted
error. Each page prints that label top and bottom; `notes` in `truth.json` repeats it per bill.

**Rebuild:** `PYTHONPATH=src python -m fairbill.gallery_gen` (fixed seed: the same values, layout and photo noise every run; PDF bytes are not guaranteed identical across reportlab versions).
**Verify:** `-m fairbill.gallery_gen --check` re-reads every price from the fixtures, exits non-zero on any mismatch.
