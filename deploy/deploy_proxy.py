"""Deploy the Node.js 22 Lambda that fronts the AgentCore Runtime.

One Function URL serves the page, its assets and the API. /api/run is streamed,
so the URL is created with InvokeMode=RESPONSE_STREAM and the handler is wrapped
in awslambda.streamifyResponse.

Idempotent: role, function, URL and public-invoke permission are created when
absent and updated in place when present. On AccessDenied it writes
_runs/2026-09-13_phase6_deploy/BLOCKER.md and exits 2 without creating anything.

    python deploy/deploy_proxy.py [--skip-bundle]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = REPO / "_runs" / "2026-09-13_phase6_deploy"
DEPLOY_JSON = RUN_DIR / "DEPLOY.json"
BLOCKER = RUN_DIR / "BLOCKER.md"
ZIP_PATH = RUN_DIR / "fairbill_proxy.zip"

REGION = "us-east-1"
FUNCTION = "fairbill-proxy"
ROLE_NAME = "fairbill-proxy-role"
RUNTIME = "nodejs22.x"
# 900s is the Lambda maximum. A live bill_02 run streams for ~450s inside the
# runtime, so the old 300s ceiling cut the SSE stream after the first event.
TIMEOUT_S = 900
MEMORY_MB = 1024


def blocked(call: str, err: ClientError, policy: str) -> None:
    msg = err.response["Error"]["Message"]
    BLOCKER.write_text(
        f"# BLOCKER - Phase 6 proxy deploy, {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"Denied call:\n\n```\n{call}\n-> {err.response['Error']['Code']}: {msg}\n```\n\n"
        f"Attach **{policy}** to the IAM user, then re-run:\n\n"
        "```\npython deploy/deploy_proxy.py\n```\n\n"
        "Nothing was created by this run.\n", encoding="utf-8")
    print(f"BLOCKED: {call} -> {msg}", file=sys.stderr)
    raise SystemExit(2)


def preflight(lam, iam) -> None:
    for client, call, kwargs, policy in (
            (lam, "list_functions", {"MaxItems": 1}, "AWSLambda_FullAccess"),
            (iam, "list_roles", {"MaxItems": 1}, "IAMFullAccess")):
        try:
            getattr(client, call)(**kwargs)
        except ClientError as e:
            blocked(f"{client.meta.service_model.service_name}:{call}", e, policy)


def ensure_role(iam, runtime_arn: str) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"role exists: {arn}")
        fresh = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Fairbill Lambda proxy role")["Role"]["Arn"]
        print(f"created role: {arn}")
        fresh = True
    iam.attach_role_policy(RoleName=ROLE_NAME,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="fairbill-invoke-runtime",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                            "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
                            # Both forms: the runtime itself and its endpoints.
                            "Resource": [runtime_arn, f"{runtime_arn}/*"]}]}))
    if fresh:
        print("waiting 12s for the role to propagate")
        time.sleep(12)
    return arn


def ensure_function(lam, role_arn: str, runtime_arn: str) -> str:
    blob = ZIP_PATH.read_bytes()
    env = {"Variables": {"AGENT_ARN": runtime_arn}}
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=blob)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        out = lam.update_function_configuration(
            FunctionName=FUNCTION, Role=role_arn, Handler="index.handler", Runtime=RUNTIME,
            Timeout=TIMEOUT_S, MemorySize=MEMORY_MB, Environment=env)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        print(f"updated function {out['FunctionArn']}")
    except lam.exceptions.ResourceNotFoundException:
        out = lam.create_function(
            FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn, Handler="index.handler",
            Code={"ZipFile": blob}, Timeout=TIMEOUT_S, MemorySize=MEMORY_MB, Environment=env,
            Description="Fairbill public page and AgentCore Runtime proxy")
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
        print(f"created function {out['FunctionArn']}")
    return out["FunctionArn"]


def ensure_url(lam) -> str:
    cfg = dict(FunctionName=FUNCTION, AuthType="NONE", InvokeMode="RESPONSE_STREAM",
               Cors={"AllowOrigins": ["*"], "AllowMethods": ["*"], "AllowHeaders": ["*"]})
    try:
        url = lam.create_function_url_config(**cfg)["FunctionUrl"]
        print("created function URL")
    except lam.exceptions.ResourceConflictException:
        url = lam.update_function_url_config(**cfg)["FunctionUrl"]
        print("updated function URL")
    # Two statements, not one: "Starting in October 2025, new function URLs will
    # require both lambda:InvokeFunctionUrl and lambda:InvokeFunction permissions"
    # (docs.aws.amazon.com/lambda/latest/dg/urls-auth.html). With only the first,
    # every anonymous request comes back 403 Forbidden.
    grants = [dict(StatementId="public-function-url", Action="lambda:InvokeFunctionUrl",
                   FunctionUrlAuthType="NONE"),
              dict(StatementId="public-invoke-function", Action="lambda:InvokeFunction",
                   InvokedViaFunctionUrl=True)]
    for g in grants:
        try:
            lam.add_permission(FunctionName=FUNCTION, Principal="*", **g)
            print(f"added {g['Action']}")
        except lam.exceptions.ResourceConflictException:
            print(f"{g['Action']} already present")
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-bundle", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from fairbill.config import load_env
    load_env()

    doc = json.loads(DEPLOY_JSON.read_text(encoding="utf-8"))
    runtime_arn = doc["runtime_arn"]

    if not a.skip_bundle:
        subprocess.run([sys.executable, str(REPO / "deploy" / "bundle_proxy.py")], check=True)

    cfg = Config(read_timeout=300, connect_timeout=20, retries={"max_attempts": 2})
    lam = boto3.client("lambda", region_name=REGION, config=cfg)
    iam = boto3.client("iam", region_name=REGION)

    preflight(lam, iam)
    role_arn = ensure_role(iam, runtime_arn)
    fn_arn = ensure_function(lam, role_arn, runtime_arn)
    url = ensure_url(lam)

    doc.update({"url": url, "proxy_function_arn": fn_arn, "proxy_role_arn": role_arn,
                "proxy_zip_bytes": ZIP_PATH.stat().st_size, "proxy_runtime": RUNTIME,
                "proxy_invoke_mode": "RESPONSE_STREAM",
                "proxy_log_group": f"/aws/lambda/{FUNCTION}"})
    DEPLOY_JSON.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\nFairbill is live at: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
