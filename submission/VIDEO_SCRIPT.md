# Fairbill film: shot list (50 seconds max)

Per `OWNER_DIRECTIVES.md` (2026-09-14 03:05 CT): the film is at most 50 seconds, a separate video session owns generation, capture and editing, and this file names the product moments that matter most. Every shot is tagged GENERATED (owner renders it in Google Flow / Veo from the prompt given) or REAL (screen capture of the live link, URL visible). Every REAL shot runs against the deployed page, never mock, never cached. Every spoken number is in the sources table at the end.

Runtime: 50 s. Generated 12 s (open, close). Real 38 s. Voice-over 124 words.

The three product moments kept: the live price-file download with the checksum, the bill line beside the hospital's own row leading to the letter, and the guard refusing to pay. Dropped from the PLAN.md never-cut list because they do not fit 50 s: the clean-bill scene and the bench table (their numbers survive as one on-screen caption in shot 4). Logged under Deviations in STATUS.md.

| # | Tag | Dur | Shot | Voice-over |
|---|---|---|---|---|
| 1 | GENERATED | 6 s | Kitchen table in a modest Oklahoma home at dusk. A person in their forties opens an envelope and unfolds a printed statement, face unreadable, paper unreadable. | "Every hospital in America has to publish its full price list. Nobody reads it." |
| 2 | REAL | 6 s | Phone, cellular, opens the live link (address bar visible). Gallery of six statements with the fictional-patient label. Tap bill 2, Norman Regional. | "Six demo statements. The patients are made up. Every price is the hospital's own." |
| 3 | REAL | 8 s | Fetch screen. The real CSV URL, byte counter climbing to 39,141,747, badge flips to `live`, then the line `sha256 4b79577f0859 matches the committed fixture`. | "It downloads the hospital's own price file, live. Thirty-nine megabytes, and the checksum matches." |
| 4 | REAL | 10 s | Audit. Four rows turn green, line 5 red. Tap it: bill line $449.00 beside the file's row 14756, cash price $269.40. Cut to the Decision Card, `$179.60 at stake`, default option marked. Caption overlay (editor adds): `Bench: 5 of 5 planted errors found, 0 false flags.` | "Line five. The bill says four hundred forty-nine. The hospital's own posted cash price is two sixty-nine forty. Plain Python re-derives every citation from the file." |
| 5 | REAL | 8 s | Tap the default. The letter scrolls; highlight the row citation (code 71046, row 14756) and `45 CFR 180.50`. The calendar button and the ledger row appear. | "One tap. A dispute letter that quotes the row and the rule, a calendar deadline, a ledger you can undo." |
| 6 | REAL | 6 s | Chat box on bill 2: `just pay it`, Send. Reply, then the trace card `pay_bill  DENIED`. | "Ask it to pay. A guard in code refuses. It has never moved a dollar." |
| 7 | GENERATED | 6 s | Same kitchen, next morning, sunlight. The person seals an envelope and sets it by the door beside car keys; small smile, no text visible anywhere. | "Fairbill. Your bill, checked against the file the hospital had to publish." |

## Flow prompts (GENERATED shots, ready to paste)

Shot 1: Photoreal, cinematic 24 fps. Interior kitchen of a modest single-story home in Norman, Oklahoma, at dusk; warm lamp light from the left, window with a flat prairie sky going violet. A person in their forties, plaid shirt, sits at a laminate table and opens a white envelope, unfolding a single printed page. Slow push-in from across the table, shallow depth of field, the paper stays out of focus and unreadable. Mood: quiet worry. No on-screen text, no logos, no readable documents, no dollar amounts. 6 seconds.

Shot 7: Photoreal, cinematic 24 fps. Same kitchen the next morning, low golden sunlight through the window, coffee cup on the table. The same person seals a plain envelope, sets it by the door on top of a stack of car keys, and pauses with a small relieved smile. Slow dolly right, handheld feel, shallow depth of field. Mood: relief, ordinary morning. No on-screen text, no logos, no readable documents, no dollar amounts. 6 seconds.

## Recording checklist (REAL shots, recording day)

1. Re-run `PYTHONPATH=src python -m fairbill.decisions --precompute`; cards bake in the run date.
2. `/api/health` on the live link must report `core_ready: true`, `mock: false`. No frame shows the word `cached`.
3. Shot 3 on Norman Regional only (the OU endpoint sends no content length); badge must read `live` and the sha line must be on screen.
4. Phone on cellular, wifi off, address bar visible, fictional-patient label visible in every phone frame.
5. Shot 4's caption numbers come from the same day's `PYTHONPATH=src python evals/run_bench.py`; do not caption a number that run did not produce.
6. The Decision Card deadline is spoken nowhere; it is displayed as the page renders it.

## Numbers used and their sources

| Number | Shot | Source |
|---|---|---|
| 39,141,747 bytes, "thirty-nine megabytes" | 3 | `data/SOURCES.md` Norman Regional row; live fetch same size, `STATUS.md` Phase 5 |
| sha256 prefix `4b79577f0859` | 3 | `data/SOURCES.md` Norman sha256 |
| $449.00 bill line, $269.40 cash price, row 14756, CPT 71046 | 4, 5 | `gallery/SPEC.md` bill 2 line 5; `STATUS.md` Phase 5 |
| $179.60 at stake | 4 | `STATUS.md` Phase 4 and 5 (449.00 minus 269.40) |
| 5 of 5 planted errors, 0 false flags | 4 caption | `STATUS.md` Phase 4 and Phase 5 evals; `evals/BENCH.md` |
| 45 CFR 180.50 | 5 | `data/SOURCES.md` |
| "every hospital has to publish its full price list" | 1 | 45 CFR 180.50, `data/SOURCES.md` |

Not spoken: the KFF debt figures (41%, 51%) stay in the README and Devpost text; the "80 percent of bills contain errors" figure has no primary source; no market-size figure anywhere.
