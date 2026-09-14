# Devpost submission form: Fairbill

Copy each block into the matching field on the Devpost form.

---

## Project name

Fairbill

## Tagline (under 80 characters)

Audits your hospital bill against the hospital's own published price file.

Character count: 74.

## Track

Everyday.

---

## Inspiration

[41% of US adults carry debt from medical or dental bills](https://www.kff.org/health-costs/kff-health-care-debt-survey/), and [51% of those say cost stopped them getting a test or treatment a doctor recommended](https://www.kff.org/health-costs/americans-challenges-with-health-care-costs/). Both figures are from the KFF Health Care Debt Survey, 2022.

The document that could help already exists. [45 CFR 180.50](https://www.ecfr.gov/current/title-45/part-180) requires every US hospital to publish a machine-readable file of its own prices. The files are public, free, and enormous. Norman Regional's is 39,141,747 bytes and 151,439 lines.

Almost nobody opens them. A hundred and fifty thousand rows of billing codes is not something a person reads. That is a job for an agent.

---

## What it does

You photograph a hospital bill. Fairbill reads it, identifies the hospital, downloads that hospital's published standard-charges file live, and puts the hospital's posted price next to every line you were charged.

It checks for duplicates, unbundled panels, upcoded visit levels, charges above the posted cash price, and out-of-network charges that federal law protects you from.

When it finds something, it shows the row. Bill line 5 says $449.00. The hospital's file, row 14756, says the cash price is $269.40. There is a link to the public file.

Then it hands you one card with one question: send the dispute letter, request the itemized bill first, or pay as billed. The recommended answer is already marked. The letter is already drafted, addressed, and cited to the regulation.

If the bill is clean, it says so and stops. That case is in the demo on purpose. An auditor that always finds something is not an auditor.

Fairbill never moves money. There are no payment fields anywhere in the product.

---

## How we built it

**Strands Agents SDK 1.55.1.**

- `Agent` with `structured_output_model` for the bill schema and for each specialist's claims.
- `@tool` for the hospital resolver, the price-file row lookup, and the code lookup, plus a decoy `pay_bill` tool that exists only to be denied. The letter draft and the calendar entry are plain Python functions, not tools.
- `Graph`, built with `GraphBuilder`, for the three specialists. `Graph` runs its nodes in parallel; `Swarm` does not. The specialists are a line matcher, a coding specialist, and a rights specialist. A context node fans out to all three. A validator node is gated on `all_dependencies_complete`, so it runs once, after all three have finished.
- `MultiAgentBase`, `MultiAgentResult`, `NodeResult` and `Status` to wrap each specialist in a node that never raises. A specialist that fails retries on the fallback model and then returns an empty claim set.
- `InterventionHandler` with `Deny` and `Proceed` for the money guard, set to `on_error = "deny"` so a broken guard fails closed.
- A second hook redacts the patient name and account numbers from traces.

**Models propose, code decides.** The specialists propose claims: a kind, the bill line numbers, and the codes involved. A claim never carries a price. A deterministic Python validator then pulls the real row out of the hospital's file and rebuilds the finding from scratch. A claim it cannot re-derive is dropped. That is why the fallback model cannot lower the evidence bar: both models pass through the same function.

**Amazon Bedrock.** Primary model `us.anthropic.claude-sonnet-4-6` (Claude Sonnet 4.6, US regional inference profile). Same-bar fallback `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Both in `us-east-1`.

**Amazon Bedrock AgentCore Runtime.** `BedrockAgentCoreApp` with an `@app.entrypoint` handler serving `/ping` and `/invocations`, deployed as a direct-code Runtime with boto3 (`deploy/deploy_runtime.py`, ARM64 zip, Python 3.13) behind a Lambda Function URL that streams each pipeline event to the page as it happens.

**Strands Evals.** The six gallery bills are the bench, with committed ground truth in `gallery/truth.json`. Two suites: one audits the printed bill, one reads the phone photo first and then audits what the reader produced. A Claude judge scores every drafted letter against the audit finding; mean 1.00 over the eleven scored rows of the 2026-09-14 bench.

**The reader.** OpenCV finds the page quad and warps it flat before the model sees it. Then two passes: a verbatim transcription, then a structured-output pass into the bill schema.

**The gallery.** Six statements rendered from real price-file rows, printed, and photographed. The patients are invented and labeled as invented on every page. The prices are real.

**The web tier.** One phone-first page with server-sent events, no framework and no CDN. FastAPI serves it locally; in production the Lambda Function URL serves it in front of the AgentCore Runtime.

---

## Challenges we ran into

**A few degrees of rotation breaks a statement.** On a wide bill table, a three-degree tilt shifts the right-hand money columns by a full row height, and the model attaches the wrong amount to the wrong line. The reader scored 3 of 6 before deskewing and 6 of 6 after (evals/READER_ABLATION.md in the repo).

**A model that silently used the wrong model.** A string was passed where a boolean was expected, and every reader call quietly ran on the fallback model. It only surfaced in review. The fix asserts the model id at the factory.

**Models will cite anything.** An early version let the specialists write their own citations. A review harness fed it forged claims and it accepted 7 of 12: duplicates across different dates, above-cash flags on insured lines, two-component unbundling, negative amounts. Every one of those is now a rule inside the validator, checked against the file.

**Hospital files are not one format.** Norman publishes a tall CSV. OU publishes a wide CSV with 467 columns through a vendor endpoint that sends no content length, so a progress bar has no total. The parser is written against the CMS data dictionary rather than against either file.

**Three codes we wanted are not priced.** 85004, 93000 and 93010 are absent from OU's file, so the unbundling demo uses the CBC components OU actually prices.

---

## Accomplishments

The reader reads 6 of 6 demo bills with every code and every amount exact.

The audit finds 5 of 5 planted errors and raises 0 false flags, including 0 on the clean bill. That result holds both from the printed bill and from the phone photo.

The decoy payment tool has never executed. Not once.

Every citation in every letter was pulled out of the hospital's own file by Python, not written by a model.

The live fetch runs on camera. 39,141,747 bytes in 4.5 seconds, with a sha256 that matches the fixture committed on 2026-09-13.

---

## What we learned

Parallel is not the interesting part of a multi-agent system. Separation of powers is. Letting models propose and letting code decide gave a harder guarantee than any amount of prompting, and it made the fallback model safe to use.

Refusing to find something is a feature. The clean bill in the gallery does more for trust than any of the five bills with errors.

The reader is the whole product. If a digit is wrong, every downstream step is confidently wrong. Getting the image geometry right mattered more than anything we did to the prompt.

---

## What's next for Fairbill

An MCP server exposing `audit_bill` so other agents can call the engine.

AgentCore Memory for standing rules, such as "always request the itemized bill first".

Background re-auditing: watch for the follow-up mail and re-run the audit when the itemized bill arrives.

Coverage beyond the five error types and the hard-coded lab panels, and a real user test with people who have a bill they cannot check.

---

## Built with

python, strands-agents, strands-evals, amazon-bedrock, amazon-bedrock-agentcore, claude, opencv, fastapi, uvicorn, pydantic, boto3, pymupdf, reportlab, pillow, numpy, server-sent-events

---

## Links

- Live demo: https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/
- Repository: `https://github.com/Yazan-O/fairbill`
- Video: `[VIDEO_URL]`
- Blog post: `[BLOG_URL]`

---

## Notes for the submitter

1. `https://github.com/Yazan-O/fairbill`, `[VIDEO_URL]` and `[BLOG_URL]` are placeholders; the live URL is filled in.
2. The live URL must be re-checked from a phone on cellular before the video is recorded.
3. The only letter-quality number is the 1.00 mean from `evals/BENCH.md` (2026-09-14); update it if the bench is re-run.
4. No market-size figure appears anywhere. Keep it that way.
5. The KFF figures must stay attributed to the 2022 KFF Health Care Debt Survey. The May 2025 tracking poll is a different population and is not used.
