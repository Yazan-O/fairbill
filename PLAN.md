# Fairbill (Everyday track): the bill auditor that reads the hospital's own price list

**What it is.** You photograph a hospital bill. Fairbill pulls that hospital's published machine-readable price file, matches every line, and shows you, next to each charge, the hospital's own posted price for that code. It flags duplicates, unbundled codes, upcoded visit levels, charges above the posted cash price, and out-of-network surprise charges. It drafts the itemized-bill request, the dispute letter, and the No Surprises Act dispute form, and asks you exactly one question: send, negotiate, or drop. Everything else happens without you.

**The wow.** A judge clicks a bill in the gallery. In under a minute they watch the agent find the hospital, download its real price file, and put the hospital's own row beside the overcharge, with a link they can open themselves. Then it hands them a finished dispute letter with the regulation cited. No upload, no account, no setup.

**Why it matters (sourced, read 2026-09-13).** 41 percent of US adults carry medical or dental debt (KFF Health Care Debt Survey, kff.org). Half of adults with that debt skipped a recommended test or treatment in the past year because of cost (KFF Health Tracking Poll, May 2025). Uninsured and self-pay patients billed at least 400 dollars over their good-faith estimate can open a federal dispute (CMS patient-provider dispute resolution, cms.gov/nosurprises). Every hospital must publish a machine-readable file of all standard charges (45 CFR 180, CMS enforcement of the 2026 template from April 1, 2026). Almost nobody reads that file. Fairbill does. The widely quoted "80 percent of bills contain errors" figure comes from billing-vendor blogs and does not enter the pitch unless a primary source is found.

**Who it is for.** Anyone who got a hospital bill they cannot check. Every hospital in the US now publishes the file this agent reads, so it works for every hospital, not just the two in the gallery.

## The demo gallery is the product

There is no public corpus of real patient bills. The gallery is built from real hospital price data with fictional patients, and the video says so on screen.

**Real data, downloaded and committed as fixtures (with fetch date and URL in `data/SOURCES.md`):**
- Norman Regional Health System standard charges CSV: `https://www.normanregional.com/documents/PFS/736048282_norman-regional-health-system_standardcharges.csv` (URL confirmed on the hospital's price transparency page, 2026-09-13; file not yet downloaded or parsed).
- OU Health (University of Oklahoma Medical Center) standard charges CSV via the Craneware public endpoint linked from ouhealth.com pricing transparency page (URL confirmed 2026-09-13; not yet downloaded).
- CMS templates and data dictionary: `github.com/CMSgov/hospital-price-transparency` (tall CSV, wide CSV, JSON schema). The parser is written against the dictionary, so any compliant hospital file loads.
- CMS No Surprises Act dispute-process guidance and the patient dispute form (cms.gov/nosurprises).

**Six gallery bills, each a PDF and a phone photo of the printed page:**

| # | Hospital | Planted error | What the agent must show |
|---|---|---|---|
| 1 | Norman Regional | Duplicate line item (same CPT, same date, billed twice) | Both lines, the price-file row, "billed twice" |
| 2 | Norman Regional | Charge above the hospital's posted discounted cash price | Bill line beside the cash-price column of the real file |
| 3 | OU Health | Unbundled panel (CBC components billed separately when the panel code exists) | The panel row in the file, the three component rows, the difference |
| 4 | OU Health | Upcoded emergency visit level (99285 billed, notes support 99283) | Visit-level row, the definition, a question to the user because this needs judgment |
| 5 | Norman Regional | Out-of-network anesthesia at an in-network facility | No Surprises Act protection cited, dispute form drafted |
| 6 | Norman Regional | Clean bill, no error | The agent says "this bill matches the hospital's file" and does nothing. This scene is mandatory; an auditor that always finds something is not trusted. |

Ground truth for all six lives in `gallery/truth.json` and drives the Strands Evals bench. Headline in the video: "found N of N planted errors, zero false flags on the clean bill, measured."

Also include one genuinely published sample statement (CMS Medicare Summary Notice sample PDF, public) to show the reader handles a real layout it never saw.

## What the judge sees, scene by scene (video is under 5 minutes)

1. **Open (20 s).** A real printed bill on a desk. "Every US hospital publishes its full price list by law. Nobody reads it. This agent does."
2. **Gallery click (10 s).** QR code on screen, judge opens the live page, picks bill 2.
3. **Read (15 s).** Fields appear with the snippet each came from: hospital, dates, each line's code and charge. A wrong field is the biggest demo risk, so every field shows its source crop.
4. **Fetch (15 s).** "Found Norman Regional Health System. Downloading its published standard charges file." Progress bar on a real download, file size shown.
5. **Audit (30 s).** Lines light up green or red. Red line: bill says X, the hospital's own file says cash price Y, row shown, link shown.
6. **Decision Card (20 s).** One card: situation, three options (send the dispute letter, request the itemized bill first, drop it), the default, the deadline. Judge taps one.
7. **Output (20 s).** The letter, addressed, cited (45 CFR 180 for the file, the hospital's own cash price, the No Surprises Act section for bill 5), ready to send. A calendar entry for the response deadline.
8. **The clean bill (15 s).** Bill 6. "Matches the file. Nothing to do." Ledger shows the check happened.
9. **Guard scene (15 s).** The agent is asked to "just pay it". A hook blocks the payment tool and shows the block in the trace. Fairbill never moves money.
10. **Bench and architecture (30 s).** Eval results table, then the diagram, then AgentCore Runtime endpoint on screen.
11. **Pitch close (30 s).** Problem, who, why. One line on what the agent does in the background: it watches for follow-up mail and re-audits when the itemized bill arrives.

## Architecture (Strands first, AgentCore where it strengthens the score)

```
photo/PDF ──▶ Reader agent (Bedrock multimodal, structured output: Bill schema, each field with source crop)
                 │
                 ▼
          Triage (tiered routing: regex on hospital name and NPI → hospital registry lookup, no model call;
                  cheap model only if the name is ambiguous)
                 │
                 ▼
     Strands Graph, parallel nodes (event-driven, not a call chain):
       ├─ PriceFile agent: download + parse the hospital's MRF (tool: mrf_fetch, mrf_lookup(code, payer))
       ├─ Coding agent: duplicates, unbundling, visit level (tools: ncci_pairs, code_lookup)
       └─ Rights agent: No Surprises Act applicability, dispute eligibility, deadlines
                 │
                 ▼
          Judgment agent: merges findings, builds the Decision Card, drafts letters (structured output)
                 │
                 ▼
     Ledger + calendar feed + Decision Card UI (web page, QR)
```

- **Strands pieces used, by name** (see `../01_AGENT_CORE_CONCEPTS.md`, `../02_TOOLS.md`, `../03_HOOKS_PLUGINS_INTERVENTIONS.md`, `../04_MULTI_AGENT.md`): `Agent` with structured output for the Bill and Findings schemas; `@tool` for `mrf_fetch`, `mrf_lookup`, `code_lookup`, `draft_letter`, `calendar_add`; `Graph` for the three parallel specialists (Graph runs nodes in parallel, Swarm does not, verified at source in the reference folder); hooks: a `BeforeToolCall` hook that denies any tool whose name or arguments touch payment, card numbers, or bank fields, and a PII redaction hook on traces; session manager for the ledger; conversation manager for long bills.
- **Same-bar fallback:** primary model Sonnet on Bedrock, fallback Haiku on Bedrock, both outputs pass the same `validate_findings()` function (every finding must cite a bill line and a price-file row, or it is dropped). The validator is one function, called on both paths.
- **Outward tool surface:** the audit engine is also exposed as an MCP server (`fairbill-mcp`) so any other agent can call `audit_bill(pdf)`. Access controlled with a token. This is the Google pattern 1 and a creativity point.
- **AgentCore:** deploy the agent on AgentCore Runtime (`../12_AGENTCORE_RUNTIME.md`, `agentcore` CLI); use AgentCore Memory for standing rules ("always request the itemized bill first"); AgentCore Observability for the trace shown in the guard scene. Gateway and Policy are optional here; skip unless time allows after the video is cut.
- **Evals:** Strands Evals (`../09_EVALS_SDK.md`) with deterministic checks against `truth.json`, plus an LLM judge on letter quality. The bench runs in CI and its table goes in the README.

## Build phases, each ends with something a judge could see

1. **Data phase.** Download both price files, parse them with the CMS dictionary, prove `mrf_lookup("85025")` returns the real row from each. Done-check: a script prints the row and the file's fetch date.
2. **Gallery phase.** Generate the six bills from real rows (render PDF, print, photograph or simulate a photo with perspective and blur). Write `truth.json`. Done-check: a human can read each photo, and the ground truth matches the rows.
3. **Reader phase.** Bill schema with source crops. Done-check: 6 of 6 bills read with every code and charge exact; below that, fix before moving on, because a misread charge kills the demo.
4. **Audit phase.** Graph with the three specialists and the validator. Done-check: bench finds 5 of 5 planted errors, 0 flags on the clean bill.
5. **Decision and drafts phase.** Decision Card UI, letters, calendar feed, guard hook, ledger. Done-check: the ten scenes above run end to end on a phone from the QR code.
6. **Deploy phase.** AgentCore Runtime endpoint, live demo link, MCP server. Done-check: the judge URL works from a phone on cellular.
7. **Ship phase.** Video, README (with bench table and SOURCES.md), architecture diagram, MIT license in About, builder.aws post ("Agents for Humans: teaching an agent to read a hospital's price file"), Devpost form.

## Cut list (cut in this order if anything slips)

MCP server, AgentCore Memory rules, the Medicare Summary Notice sample, bill 4 (the judgment case). Never cut: the real price-file fetch on camera, the clean-bill scene, the guard scene, the bench table.

## Risks and how the plan answers them

- **Price files are huge and inconsistently formatted.** Parse against the CMS dictionary, cache a filtered slice (only the codes in the gallery plus common ones) as a committed fixture, and fetch live on camera with a timeout that falls back to the fixture. Say which one ran.
- **A misread digit.** Every field shows its crop; the reader is tested to exactness before anything else is built.
- **Judges suspect fake data.** Every bill carries "constructed from the hospital's published prices, patient fictional," and every flagged row links to the public file.
- **Legal tone.** Letters cite regulations but the product never claims to be legal advice; a footer says so.
