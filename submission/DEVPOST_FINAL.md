# Devpost submission: Fairbill

Paste each section into the matching Devpost form field, in order.

---

## Project name

Fairbill

## Elevator pitch

Audits your hospital bill against the hospital's own published price file.

## About the project

## Inspiration

A hospital bill arrives as a sheet of codes and dollar amounts. [41% of US adults carry debt from medical or dental bills](https://www.kff.org/health-costs/kff-health-care-debt-survey/), and [51% of those say cost stopped them getting a test or treatment a doctor recommended](https://www.kff.org/health-costs/americans-challenges-with-health-care-costs/) (KFF Health Care Debt Survey, 2022).

The document that could help already exists. [45 CFR 180.50](https://www.ecfr.gov/current/title-45/part-180) requires every US hospital to publish a machine-readable file of its own prices. The files are public, free, and enormous. Norman Regional Health System's is 39,141,747 bytes and 151,439 lines. OU Health publishes a wide CSV with 467 columns through a vendor endpoint. Almost nobody opens them. A hundred and fifty thousand rows of billing codes is not something a person reads. That is a job for an agent.

## What it does

You photograph a hospital bill. Fairbill reads it, identifies the hospital, downloads that hospital's published standard-charges file live, and puts the hospital's posted price next to every line you were charged.

It checks five things: the same code billed twice, a lab panel split into its components, a visit level billed higher than the notes support, a charge above the posted cash price, and an out-of-network charge that federal law protects you from.

When it finds something, it shows the row. Bill line 5 says $449.00. The hospital's file, row 14756, says the cash price is $269.40. Then it hands you one card: send the dispute letter, request the itemized bill first, or pay as billed. The letter is already drafted, cited to the regulation, and addressed.

If the bill is clean, it says so. That case is in the demo on purpose. An auditor that always finds something is not an auditor.

Fairbill never moves money. There are no payment fields anywhere in the product.

## How we built it

Three specialists run in parallel inside a Strands `Graph`: a line matcher, a coding specialist, and a rights specialist. Each proposes claims using structured output. A claim names a kind and the relevant billing codes. It never carries a price or a citation. A deterministic Python validator then re-derives every finding from the hospital's own file. If it cannot rebuild the claim, the claim is dropped. That is why the fallback model cannot lower the evidence bar: the bar is in the code, not the model.

A context node fans out to the three specialists, which converge on the validator gated by `all_dependencies_complete`. Each specialist subclasses `MultiAgentBase` and never raises: a failed specialist retries on the fallback model and returns an empty claim set.

The reader uses OpenCV to find the page quad and warp it flat before the model sees anything. Two passes follow: a verbatim transcription, then a structured-output pass into a `Bill` schema. Deskew took the reader from [3 of 6 to 6 of 6](https://github.com/Yazan-O/fairbill/blob/main/evals/READER_ABLATION.md).

A Strands `InterventionHandler` with `on_error = "deny"` guards every tool call. It denies anything that touches payment verbs, card-shaped digits, or routing numbers. A decoy `pay_bill` tool exists to be denied on camera. Its execution counter has never left zero.

Primary model is Claude Sonnet 4.6 on Amazon Bedrock (`us.anthropic.claude-sonnet-4-6`). Fallback is Claude Haiku 4.5, same region. Both pass through the same validator.

The engine runs on an Amazon Bedrock AgentCore Runtime (direct-code zip, Python 3.13, ARM64) behind a Lambda Function URL in streaming mode. One URL, one QR code.

## Challenges we ran into

A few degrees of rotation breaks a statement. On a wide bill table, a three-degree tilt shifts the money columns by a full row height and the model attaches the wrong amount to the wrong line. The fix was geometry (OpenCV deskew), not a better prompt.

Models will cite anything. A review harness fed the first version forged claims and it accepted 7 of 12: cross-date duplicates, above-cash flags on insured lines, two-component unbundling, negative amounts. Each is now a rule in the validator.

Hospital files are not one format. Norman publishes a tall CSV. OU publishes a wide CSV with 467 columns. The parser is written against the CMS data dictionary rather than either file.

## Accomplishments that we're proud of

The reader reads 6 of 6 demo bills with every code and every amount exact. The audit finds 5 of 5 planted errors and raises 0 false flags, including 0 on the clean bill, from both the printed bill and the phone photo (10 of 10 overall). Every drafted letter scores 1.00 from the Claude judge. The decoy payment tool has never executed.

## What we learned

Parallel is not the interesting part of a multi-agent system. Separation of powers is. Letting models propose and letting code decide gave a harder guarantee than any amount of prompting, and it made the fallback model safe to use.

The reader is the whole product. If a digit is wrong, every downstream step is confidently wrong. Getting the image geometry right mattered more than anything we did to the prompt.

Refusing to find something is a feature. The clean bill does more for trust than any of the five bills with errors.

## What's next for Fairbill

An MCP server exposing `audit_bill` so other agents can call the engine. AgentCore Memory for standing rules, such as "always request the itemized bill first." Background re-auditing when the itemized bill arrives. Coverage beyond the five error types and the hard-coded lab panels, and a real user test with people who have a bill they cannot check.

## Built with

python, strands-agents, strands-evals, amazon-bedrock, amazon-bedrock-agentcore, claude-sonnet-4.6, claude-haiku-4.5, aws-lambda, node.js, opencv, fastapi, uvicorn, pydantic, boto3, pymupdf, reportlab, pillow, numpy, server-sent-events

## Try it out links

- Live demo: https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/
- GitHub repo: https://github.com/Yazan-O/fairbill
- Gallery page: https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/

## Image gallery

1. **submission/thumbnail.png** -- Fairbill thumbnail for the Devpost listing.
2. **submission/architecture.png** -- Architecture diagram: phone to Lambda to AgentCore Runtime to Bedrock, with the Strands Graph of three specialists, the deterministic validator, and the guard boundary.
3. **submission/media/hero_phone.png** -- The Decision Card on a phone: $179.60 at stake, three options, "Send the dispute letter" recommended, a deadline, and a link to the hospital's own price file.
4. **submission/media/audit_row_phone.png** -- A flagged audit row showing the billed price next to the price in the hospital's own file, with the file row number.
5. **submission/media/flow.gif** -- An 18-second time-lapse of a full live run: statement read, price file fetched with sha256, flagged row, Decision Card, dispute letter drafted, then "just pay it" denied by the guard.

## Video demo link

[VIDEO_URL]

## Submitter Type

Individual

## Country of Residence

United States

## Organization



## Track

Everyday

## PUBLIC URL to your code repo

https://github.com/Yazan-O/fairbill

## Architecture diagram

submission/architecture.png

## AWS Builder ID

[OWNER FILLS]

## Live demo link

https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/

## Testing instructions

No login, no AWS account, and no install needed. Open the link on any device.

1. Open the live demo: https://waixxctuxe5ojzlndpci5wvpqi0bixad.lambda-url.us-east-1.on.aws/
2. Tap the second statement card, **Norman Regional Health System, Outpatient diagnostics** (patients fictional, prices real). The run starts on the tap.
3. Watch the agent read the statement, download the hospital's 39 MB price file live (the sha256 is shown), and audit each line. Progress updates appear as each step finishes.
4. See the flagged row: bill line 5 charged $449.00, the hospital's own file says $269.40 on row 14756.
5. Read the Decision Card: $179.60 at stake, three options, "Send the dispute letter" recommended, with a deadline and a link to the evidence.
6. Tap "Send the dispute letter" and read the drafted letter. It cites the regulation and the row number in the hospital's file.
7. Type **just pay it** in the chat box. The agent refuses to execute the payment tool. The guard denies the call and logs why.
8. Go back and tap the last card, **Norman Regional Health System, Outpatient lab**. That bill is clean. The agent finds nothing wrong and says so.

## Bonus blog post URL

[BLOG_URL, post submission/BLOG_builder_aws.md on builder.aws with "Agents for Humans" in the title]
