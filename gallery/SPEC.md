# Gallery spec (lead's design; builders implement exactly this)

Six fictional patient statements built from real rows of two real hospital price files. Every price on every bill is a row in `data/fixtures/*_slice.json` (which carry source URL, fetch date, raw sha256, and the raw CSV line). Patients, account numbers, dates, patient addresses, insurers, physician groups and the errors are fictional. Every page prints, top and bottom: "DEMO STATEMENT. Patient is fictional. Prices are the hospital's own published standard charges (fetched 2026-09-13)."

## Files produced
- `gallery/bills/bill_0N.pdf` (Letter, 1 page, realistic hospital statement layout; two different layouts, one per hospital, plus an insured layout for bill 5)
- `gallery/bills/bill_0N_page.png` (clean render, 200 dpi)
- `gallery/bills/bill_0N_photo.jpg` (simulated phone photo: 2 to 4 degree rotation, mild perspective, slight blur, sensor noise, warm cast, soft shadow, desk background; text must stay readable to a human at phone size)
- `gallery/bills/sample_msn_part_b_page1.png` (page 1 of `data/samples/cms_sample_part_b_msn.pdf`, real public CMS sample, not fictional)
- `gallery/truth.json`: list of entries `{id, hospital_id, files:{pdf,page,photo}, bill: <Bill per src/fairbill/schema.py, with crops>, planted: [<Finding per schema, with FileRow evidence>], clean: bool}`. Crops are pixel boxes on `_page.png` (the photo has different geometry; the Reader is scored on values, not boxes).
- `src/fairbill/gallery_gen.py`: deterministic generator (fixed seed) that reads the tables below, looks each row up in the fixtures by (code, source_line) to pull the real gross/cash/description, renders, photographs, and writes truth.json. `python -m fairbill.gallery_gen` rebuilds everything. It must fail loudly if a referenced fixture row is missing or its price differs from this spec.

## Price basis
- Norman Regional: self-pay bills show `Charge` (gross) and `Your price (self-pay)` = the file's `discounted_cash` (60% of gross in the file). Footer: "Self-pay discount applied per hospital financial assistance policy".
- OU Health: same two columns; the file's `discounted_cash` is 10% of gross. Print what the file says; do not "fix" it.
- Amounts in truth.json use the basis the bill uses: `patient_price` for self-pay bills (basis "cash"), `charge` for the insured bill (basis "gross").

## Patients (fictional; use exactly these)
1 "Jordan Sample", 2 "Casey Example", 3 "Riley Demo", 4 "Morgan Placeholder", 5 "Taylor Fictional", 6 "Alex Specimen". Account numbers `FB-2026-00000N`. Patient address "123 Demo Street, Norman, OK 73069" (bills 1, 2, 5, 6) or "456 Demo Avenue, Oklahoma City, OK 73104" (bills 3, 4). Hospital name and address come from `data/hospitals.json` (Norman Regional: billing_address; OU Health: 700 NE 13th St, Oklahoma City, OK 73104).

## Bill 1: Norman Regional, Emergency visit 2026-08-14, Self-pay. Planted: DUPLICATE
| line | code | fixture source_line | description on bill | charge | your price |
|---|---|---|---|---|---|
| 1 | 99283 | 16377 | ED Services Level 3 | 1729.00 | 1037.40 |
| 2 | 36415 | 16614 | ED Venipuncture, Routine | 44.00 | 26.40 |
| 3 | 85025 | 13220 | Complete Blood Count (CBC) | 140.00 | 84.00 |
| 4 | 85025 | 13220 | Complete Blood Count (CBC) | 140.00 | 84.00 |
| 5 | 80053 | 10980 | Comprehensive Metabolic Panel | 275.00 | 165.00 |
| 6 | 71046 | 14756 | Chest X-ray, 2 views | 449.00 | 269.40 |
| 7 | J1885 | 5378 | Ketorolac 15 mg injection | 48.82 | 29.29 |
| 8 | 96372 | 5911 | Therapeutic injection, IM | 221.00 | 132.60 |

Planted finding: kind duplicate, line_nos [3, 4], amount_at_stake 84.00, basis cash, evidence row 13220. Clean otherwise.

## Bill 2: Norman Regional, Outpatient diagnostics 2026-07-22, Self-pay. Planted: ABOVE_CASH
| line | code | fixture source_line | description on bill | charge | your price |
|---|---|---|---|---|---|
| 1 | 36415 | 13718 | Venipuncture | 46.00 | 27.60 |
| 2 | 84443 | 10950 | Thyroid Stimulating Hormone | 248.00 | 148.80 |
| 3 | 83036 | 12018 | Hemoglobin A1c | 135.00 | 81.00 |
| 4 | 93005 | 17430 | ECG tracing, 12 lead | 409.00 | 245.40 |
| 5 | 71046 | 14756 | Chest X-ray, 2 views | 449.00 | 449.00 |

Line 5's "your price" is printed at gross (the discount was not applied). Planted: kind above_cash, line_nos [5], amount_at_stake 179.60 (449.00 minus 269.40), basis cash, evidence row 14756.

## Bill 3: OU Health, Outpatient lab 2026-08-03, Self-pay. Planted: UNBUNDLED
| line | code | fixture source_line | description on bill | charge | your price |
|---|---|---|---|---|---|
| 1 | 36415 | 4452 | Collection of venous blood, venipuncture | 90.00 | 9.00 |
| 2 | 85027 | 6848 | Complete blood cell count, automated | 257.00 | 25.70 |
| 3 | 85018 | 4038 | Hemoglobin | 113.00 | 11.30 |
| 4 | 85014 | 4764 | Red blood cell concentration (hematocrit) | 123.00 | 12.30 |
| 5 | 85007 | 4307 | Microscopic exam, white cells, manual differential | 636.00 | 63.60 |
| 6 | 80053 | 6886 | Blood test, comprehensive group of chemicals | 1025.00 | 102.50 |

Planted: kind unbundled, line_nos [2, 3, 4, 5], evidence rows 6833 (85025 panel, gross 371.00, cash 37.10) plus 6848, 4038, 4764, 4307. amount_at_stake at cash basis = (25.70 + 11.30 + 12.30 + 63.60) minus 37.10 = 75.80. Summary: the four lines are the components of one CBC with differential (CPT 85025), which the hospital prices as one row.

## Bill 4: OU Health, Emergency visit 2026-08-21, Self-pay. Planted: UPCODED (needs user judgment)
| line | code | fixture source_line | description on bill | charge | your price |
|---|---|---|---|---|---|
| 1 | 99285 | 4045 | Emergency department visit, life-threatening or high severity | 6795.00 | 679.50 |
| 2 | 36415 | 4452 | Collection of venous blood, venipuncture | 90.00 | 9.00 |
| 3 | 80053 | 6886 | Blood test, comprehensive group of chemicals | 1025.00 | 102.50 |
| 4 | 71046 | 4603 | X-ray of chest, 2 views | 683.00 | 68.30 |
| 5 | 93005 | 3949 | Routine electrocardiogram, 12 leads, tracing | 532.00 | 53.20 |
| 6 | J7030 | 4341 | Normal saline solution infusion | 7.00 | 0.70 |

The statement carries a "Visit summary" box (fictional): "Reason for visit: sore throat and cough, 2 days. Vitals normal, no fever. Rapid strep negative. Chest X-ray clear. Discharged home with instructions; no prescriptions." Planted: kind upcoded, line_nos [1], needs_user_judgment true, evidence rows 4045 (99285) and 4688 (99283, gross 1898.00, cash 189.80), amount_at_stake 489.70 (679.50 minus 189.80), basis cash. Summary: the note describes a low-complexity visit; the bill uses the highest level.

## Bill 5: Norman Regional, Outpatient procedure 2026-06-10, Insured ("Sooner Plains Health Plan (fictional)", in-network facility). Planted: OUT_OF_NETWORK
Facility section:
| line | code | fixture source_line | description on bill | billed | allowed | plan paid | you owe |
|---|---|---|---|---|---|---|---|
| 1 | 45378 | 15227 | Colonoscopy, diagnostic | 2506.00 | 1800.00 | 1440.00 | 360.00 |
| 2 | J7030 | 5825 | Sodium chloride 1000 mL bag | 86.66 | 60.00 | 48.00 | 12.00 |

Professional section, provider "Red River Anesthesia Associates (fictional)", printed note "OUT OF NETWORK: this provider is not contracted with your plan":
| line | code | fixture source_line | description on bill | billed | allowed | plan paid | you owe |
|---|---|---|---|---|---|---|---|
| 3 | 00812 | none (professional service, not in the facility file) | Anesthesia for screening colonoscopy | 1850.00 | 0.00 | 0.00 | 1850.00 |

Planted: kind out_of_network, line_nos [3], amount_at_stake 1850.00, basis gross, regulation "No Surprises Act, 45 CFR 149.420 (non-emergency services by nonparticipating providers at participating facilities; anesthesiology cannot be balance-billed)", evidence empty (no facility row exists; the finding rests on the network note). needs_user_judgment false. Summary: anesthesia at an in-network facility is protected; the patient owes at most in-network cost sharing. The allowed/paid figures for lines 1 and 2 are fictional plan terms and are labeled so in truth.json notes.

## Bill 6: Norman Regional, Outpatient lab 2026-07-08, Self-pay. CLEAN
| line | code | fixture source_line | description on bill | charge | your price |
|---|---|---|---|---|---|
| 1 | 36415 | 13718 | Venipuncture | 46.00 | 27.60 |
| 2 | 85025 | 13220 | Complete Blood Count (CBC) | 140.00 | 84.00 |
| 3 | 80053 | 10980 | Comprehensive Metabolic Panel | 275.00 | 165.00 |
| 4 | 84443 | 10950 | Thyroid Stimulating Hormone | 248.00 | 148.80 |
| 5 | 81001 | 10912 | Urinalysis, automated with microscopy | 164.00 | 98.40 |

Planted: none. clean true.

## Layout notes
- Norman layout: hospital name block top-left, "Patient Statement" title, account / statement date / service dates block, table with columns Date | Code | Description | Qty | Charge | Your price, totals (Total charges, Self-pay discount, Amount due), payment stub at bottom (no card fields; one line "Pay online at the hospital's billing portal"). Fictional label lines top and bottom.
- OU layout: different typography and column order (Description | CPT/HCPCS | Date | Units | Charges | Self-pay price), summary box at right, visit summary box (bill 4 only).
- Insured layout (bill 5): columns Date | Code | Description | Billed | Allowed | Plan paid | You owe; a network status note under the professional section.
- Codes are printed as plain 5-character codes; revenue codes are not printed.
- Statement date = service date + 21 days. Due date = statement date + 30 days.
- Fonts: standard reportlab fonts are fine (Helvetica for Norman, Times for OU) so the two layouts look different.
