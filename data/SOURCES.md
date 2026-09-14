# Data sources

Every fixture in `data/` and every number in the pitch traces to a row below. Raw downloads live in `data/raw/` (gitignored, large); committed fixtures are the slices in `data/fixtures/`.

## Hospital standard-charge files (real, public, 45 CFR 180)

| Hospital | File | Fetched | Layout | Bytes | sha256 |
|---|---|---|---|---|---|
| Norman Regional Health System (EIN 73-6048282) | https://www.normanregional.com/documents/PFS/736048282_norman-regional-health-system_standardcharges.csv | 2026-09-13 | CMS v3.0.0 tall CSV, 151,439 lines, `last_updated_on` 2026-03-01 | 39,141,747 | 4b79577f0859aadc9ab9db6aa53b221edf433592ed1406c201a3336b5156bf9f |
| OU Health, University of Oklahoma Medical Center (EIN 82-1883948) | https://apim.services.craneware.com/api-pricing-transparency/api/public/ebdcd27316bbcd47fb02c60d601d05cc/charges/mrf (zip) | 2026-09-13 | CMS v3.0.0 wide CSV, 199,365 lines, 467 columns, `last_updated_on` 7/29/2026 | zip 27,791,301; csv 1,523,992,244 | zip c8cccdbbc5f2df4ab913350bb00be963772957572efb4a89cce653bd20879c9a; csv efdeac51be0d9f7c853313345bab34956541c93e737c7204733bd576e34e2099 |

Transparency pages that link the files (verified HTTP 200 on 2026-09-13):
- Norman Regional: https://www.normanregional.com/pay-your-bill/price-transparency/
- OU Health: https://www.ouhealth.com/ou-health-patients-families/insurance-billing/billing-ou-health/hospital-charges-pricing-transparency/

Fixtures: `data/fixtures/norman_regional_slice.json` and `data/fixtures/ou_health_slice.json` are slices of the raw files restricted to the 69 unique codes in `data/gallery_codes.txt` (the list has 70 entries; 70450 appears twice), built by `python -m fairbill.mrf slice`; each carries `source_url`, `fetched_on`, `raw_sha256`, and the file's own header rows. Hospital registry (NPIs, EINs, addresses, file URLs): `data/hospitals.json`. Norman billing address "Norman Regional Hospital Authority P.O. Box 268961 Oklahoma City, OK 73126" quoted from https://www.normanregional.com/patients-visitors/pay-your-bill/ (fetched 2026-09-13); phone numbers printed on the demo bills are reserved 555-01xx numbers labeled fictional.

## CMS Hospital Price Transparency data dictionary
- Repo: https://github.com/CMSgov/hospital-price-transparency (fetched 2026-09-13). Copied into `data/cms/`: `CSV_README.md` (data dictionary v3.0), `V3.0.0_schema.json`, `HOW_TO_READ_DATA_DICTIONARY.md`. The parser `src/fairbill/mrf.py` implements the tall and wide layouts as this dictionary defines them.
- Regulation: 45 CFR Part 180 (Hospital Price Transparency).

## Pitch numbers (primary sources, each opened and quoted 2026-09-13)
- "41% of adults currently have some debt caused by medical or dental bills." KFF Health Care Debt Survey (fielded Feb-Mar 2022, published 2022-06-16): https://www.kff.org/health-costs/kff-health-care-debt-survey/
- "Half (51%) of adults currently experiencing debt due to medical or dental bills say in the past year, cost has been a prohibitor to getting the medical test or treatment that was recommended by a doctor." Same survey, as quoted on KFF "Americans' Challenges with Health Care Costs" (updated 2026-04-30): https://www.kff.org/health-costs/americans-challenges-with-health-care-costs/
- "About one-third (36%) of adults say that in the past 12 months they have skipped or postponed getting health care they needed because of the cost." KFF Health Tracking Poll, May 2025, quoted on the same KFF page.
- Patient-provider dispute: "One of your providers or facilities charged at least $400 more than their good faith estimate" and "You have an initial bill dated within the last 120 calendar days." CMS, "Dispute a medical bill": https://www.cms.gov/medical-bill-rights/help/dispute-a-bill
- Every hospital must publish a machine-readable file of standard charges: 45 CFR 180.50, https://www.ecfr.gov/current/title-45/part-180 ; the CMS Hospital Price Transparency page states the rule took effect January 1, 2021 (fetched 2026-09-14); CMS Hospital Price Transparency page: https://www.cms.gov/priorities/key-initiatives/hospital-price-transparency (the "2026 template enforced from April 1, 2026" claim in PLAN.md is not yet quoted from a primary page; it stays out of the video until it is).

## Sample statement (real, public)
- CMS sample Medicare Summary Notice, Part A: https://www.cms.gov/medicare/medicare-general-information/msn/downloads/sample-part-a-medicare-summary-notice.pdf fetched 2026-09-13, 383,391 bytes, sha256 132e3b3aa2933bf01891e6c57674ed3babb98d013eb2673b44efbc350c011199, saved as `data/samples/cms_sample_part_a_msn.pdf`.
- CMS sample Medicare Summary Notice, Part B: https://www.medicare.gov/pubs/pdf/summarynoticeb.pdf fetched 2026-09-13, 382,449 bytes, sha256 75b62e9f19411aafd2f6161a8543d066b877e16f1b84825193b2eee006a1a9b9, saved as `data/samples/cms_sample_part_b_msn.pdf`. Linked from https://www.cms.gov/medicare/coverage/summary-notice.



## Fictional parts
Patients, addresses, account numbers, dates of service, and the billing errors on the six gallery bills are fictional and labeled as such on every bill, on the web page, and in the README. The prices on those bills are real rows from the files above.
- No Surprises Act notice-and-consent rule cited in the letters: 45 CFR 149.420, https://www.ecfr.gov/current/title-45/part-149/section-149.420 (fetched 2026-09-14).
- Reader ablation (3 of 6 without deskew, 6 of 6 with, 2026-09-13): `evals/READER_ABLATION.md`.
