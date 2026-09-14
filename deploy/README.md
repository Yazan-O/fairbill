# Fairbill deploy

Two AWS resources serve the whole demo:

1. **AgentCore Runtime `fairbill`** runs `src/fairbill/runtime.py` from a code artifact
   (a zip in S3, Python 3.13, ARM64). No Docker, no ECR, no CloudFormation.
2. **Lambda `fairbill-proxy`** (Node.js 22) holds the public Function URL. It serves
   `web/index.html`, the gallery images and the read-only API from files baked into the
   function package, and proxies everything else to the Runtime. `/api/run` is streamed
   with `awslambda.streamifyResponse`, so the browser sees each pipeline event as it
   happens.

The `agentcore` CLI is not used: it is CDK-based and needs CloudFormation. Every call
here is plain boto3.

## Commands, in order

```bash
python deploy/build_runtime_zip.py     # resolve deps, install aarch64 wheels, build the zip
python deploy/deploy_runtime.py        # role + bucket + upload + create/update runtime, wait READY
python deploy/smoke_runtime.py         # every action against the deployed runtime, live streams
python deploy/bundle_proxy.py          # stage web/, gallery/, pre-baked API JSON, zip
python deploy/deploy_proxy.py          # role + function + Function URL (RESPONSE_STREAM)
python deploy/smoke_proxy.py           # the same journey over the public URL
```

`deploy_runtime.py` runs the build itself unless given `--skip-build`; `deploy_proxy.py`
runs the bundle itself unless given `--skip-bundle`. Every script is idempotent: run any
of them again after a change and it updates in place.

Local handler test, no AWS involved, against a runtime started from the extracted
artifact:

```bash
python -c "import zipfile;zipfile.ZipFile('_runs/2026-09-13_phase6_deploy/fairbill_runtime.zip').extractall('/tmp/layout')"
python /tmp/layout/main.py &
FAIRBILL_LOCAL_RUNTIME=http://127.0.0.1:8080/invocations node deploy/test_proxy.mjs
```

## Files

| path | what it does |
| --- | --- |
| `build_runtime_zip.py` | deterministic artifact: 644 files / 755 dirs, no `__pycache__`, ARM64 ELF check, size report |
| `deploy_runtime.py` | `fairbill-runtime-role`, bucket `fairbill-runtime-<acct>-us-east-1`, `create/update_agent_runtime`, writes `DEPLOY.json` |
| `smoke_runtime.py` | `invoke_agent_runtime` for bills / run / decide / ics / chat / ledger / undo; transcripts to `_runs/2026-09-13_phase6_deploy/smoke_*.txt` |
| `prebake.py` | captures `/api/bills`, `/api/bench`, `/api/health`, truth-crops from the real FastAPI app |
| `bundle_proxy.py` | stages `web/` and `gallery/bills/` into the function package and zips it |
| `lambda_proxy/index.mjs` | the Function URL handler |
| `test_proxy.mjs` | the handler under fake Function-URL events, in process |
| `deploy_proxy.py` | `fairbill-proxy-role`, function `fairbill-proxy`, Function URL, appends `url` to `DEPLOY.json` |
| `smoke_proxy.py` | the public URL end to end |

The Lambda timeout is 900s, the service maximum: a live run streams for a minute or more
and a shorter ceiling cuts the SSE stream mid-run.

## What is proven, and what is not

Proven by a command whose output is in `_runs/2026-09-13_phase6_deploy/`:

- the artifact's layout runs: the zip extracted and served `/ping` and `/invocations` for
  every action (`layout_proof.txt`);
- every bundled shared object is ARM64 (`build_runtime_zip.txt`);
- the deployed runtime reaches READY and answers every action (`smoke_*.txt`);
- the runtime streams progressively, seconds to minutes between frames (`smoke_run_bill_06.txt`);
- the proxy handler serves the page, assets, pre-baked API and all runtime routes
  (`test_proxy_local.txt`, `smoke_proxy.txt`).

Not proven here:

- the page in a real browser on a phone over cellular. That is the demo gate in
  `PLAN.md` and has to be done by opening the URL on a handset.
- cold-start latency under load; only single sequential invocations were measured.

## Rollback

```bash
aws lambda delete-function-url-config --function-name fairbill-proxy --region us-east-1
aws lambda delete-function --function-name fairbill-proxy --region us-east-1
aws iam delete-role-policy --role-name fairbill-proxy-role --policy-name fairbill-invoke-runtime
aws iam detach-role-policy --role-name fairbill-proxy-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name fairbill-proxy-role

aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <runtime_id> --region us-east-1
aws iam delete-role-policy --role-name fairbill-runtime-role --policy-name fairbill-runtime
aws iam delete-role --role-name fairbill-runtime-role
aws s3 rb s3://fairbill-runtime-<acct>-us-east-1 --force
```

`<runtime_id>` and the bucket name are in `_runs/2026-09-13_phase6_deploy/DEPLOY.json`.

## Notes

- Credentials never enter the artifact. `.env` is not in either zip; the runtime signs
  Bedrock calls with its execution role, and the Lambda with its own.
- Logs: `/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT` and `/aws/lambda/fairbill-proxy`.
- `update_agent_runtime` is a full replace, so `deploy_runtime.py` always resends the
  role, artifact, network, protocol and environment together.
- A browser session id arrives in `X-Fairbill-Session` and is padded to 40 characters
  before it becomes the `runtimeSessionId`, which must be at least 33.
