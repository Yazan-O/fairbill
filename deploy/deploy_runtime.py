"""Deploy src/fairbill/runtime.py to Bedrock AgentCore Runtime as a code artifact.

Idempotent: the execution role, the artifact bucket and the runtime are each
created when absent and updated in place when present. No Docker and no
CloudFormation, so this works with only bedrock-agentcore, s3 and iam
permissions -- the `agentcore` CLI is CDK-based and needs CloudFormation, which
this account denies.

Preflight probes every permission it needs and, on AccessDenied, writes
_runs/2026-09-13_phase6_deploy/BLOCKER.md and exits 2 without creating anything.

    python deploy/deploy_runtime.py [--skip-build]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = REPO / "_runs" / "2026-09-13_phase6_deploy"
ZIP_PATH = RUN_DIR / "fairbill_runtime.zip"
DEPLOY_JSON = RUN_DIR / "DEPLOY.json"
BLOCKER = RUN_DIR / "BLOCKER.md"

REGION = "us-east-1"
AGENT_NAME = "fairbill"
ROLE_NAME = "fairbill-runtime-role"
PREFIX = "fairbill/runtime.zip"
POLL_SECONDS = 15

ENV_VARS = {
    "MODEL_PRIMARY": "us.anthropic.claude-sonnet-4-6",
    "MODEL_FALLBACK": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "AWS_REGION": REGION,
    "BYPASS_TOOL_CONSENT": "true",
    # /tmp is the only writable path in the microVM.
    "FAIRBILL_LEDGER_DIR": "/tmp/fairbill/ledger",
    "FAIRBILL_CACHE_DIR": "/tmp/fairbill/fetch_cache",
    # 20s (the local default) is not enough for the 39 MB file from us-east-1.
    "FAIRBILL_FETCH_TIMEOUT": "120",
}


def blocked(call: str, err: ClientError, policy: str) -> None:
    """Record the exact denial and stop. Nothing else is created."""
    msg = err.response["Error"]["Message"]
    BLOCKER.write_text(
        f"# BLOCKER - Phase 6 runtime deploy, {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"Denied call:\n\n```\n{call}\n-> {err.response['Error']['Code']}: {msg}\n```\n\n"
        f"Missing permission. Attach **{policy}** to the IAM user, then re-run:\n\n"
        "```\npython deploy/deploy_runtime.py\n```\n\n"
        "Nothing was created by this run: no role, no bucket, no runtime.\n",
        encoding="utf-8")
    print(f"BLOCKED: {call} -> {msg}", file=sys.stderr)
    print(f"wrote {BLOCKER}", file=sys.stderr)
    raise SystemExit(2)


def preflight(sts, s3, iam, ctl, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("403", "AccessDenied", "AccessDeniedException"):
            blocked(f"s3:HeadBucket on {bucket}", e, "AmazonS3FullAccess")
        if code in ("404", "NoSuchBucket"):
            try:
                s3.create_bucket(Bucket=bucket)
                print(f"created bucket {bucket}")
            except ClientError as e2:
                if e2.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException"):
                    blocked(f"s3:CreateBucket on arn:aws:s3:::{bucket}", e2, "AmazonS3FullAccess")
                if e2.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                    raise
    for client, call, kwargs in ((iam, "list_roles", {"MaxItems": 1}),
                                 (ctl, "list_agent_runtimes", {"maxResults": 1})):
        try:
            getattr(client, call)(**kwargs)
        except ClientError as e:
            blocked(f"{client.meta.service_model.service_name}:{call}", e,
                    "IAMFullAccess" if client is iam else "BedrockAgentCoreFullAccess")


def ensure_role(iam, account: str) -> str:
    """Execution role for the runtime. The trust policy conditions are the ones
    runtime-permissions.md gives for a runtime execution role."""
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:*"}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "BedrockModelInvocation", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                      f"arn:aws:bedrock:*:{account}:inference-profile/*",
                      f"arn:aws:bedrock:*:{account}:application-inference-profile/*"]},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                    "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
         "Resource": [f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"]},
        {"Sid": "Telemetry", "Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"],
         "Resource": "*"},
        {"Sid": "Workload", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": [f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default",
                      f"arn:aws:bedrock-agentcore:{REGION}:{account}:workload-identity-directory/default/workload-identity/{AGENT_NAME}-*"]}]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        print(f"role exists: {arn}")
        fresh = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Fairbill AgentCore Runtime execution role")["Role"]["Arn"]
        print(f"created role: {arn}")
        fresh = True
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="fairbill-runtime",
                        PolicyDocument=json.dumps(policy))
    if fresh:
        print("waiting 15s for the role to propagate")
        time.sleep(15)
    return arn


def upload(s3, bucket: str) -> int:
    size = ZIP_PATH.stat().st_size
    t0 = time.time()
    s3.upload_file(str(ZIP_PATH), bucket, PREFIX)
    print(f"uploaded {size:,} bytes to s3://{bucket}/{PREFIX} in {time.time() - t0:.1f}s")
    return size


def artifact(bucket: str) -> dict:
    # entryPoint carries no opentelemetry-instrument: the ADOT packages are not
    # bundled, and the doc's sample notes "if not adding otel dependency, remove
    # opentelemetry-instrument from entrypoint array".
    return {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": PREFIX}},
                                 "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}}


def ensure_runtime(ctl, role_arn: str, bucket: str) -> dict:
    existing = None
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == AGENT_NAME:
                existing = r
    common = dict(agentRuntimeArtifact=artifact(bucket), roleArn=role_arn,
                  networkConfiguration={"networkMode": "PUBLIC"},
                  protocolConfiguration={"serverProtocol": "HTTP"},
                  environmentVariables=ENV_VARS)
    if existing:
        # update_agent_runtime is a full replace, so every field goes back up.
        out = ctl.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **common)
        print(f"updated runtime {existing['agentRuntimeId']} -> version {out.get('agentRuntimeVersion')}")
    else:
        out = ctl.create_agent_runtime(agentRuntimeName=AGENT_NAME,
                                       description="Fairbill hospital-bill audit runtime", **common)
        print(f"created runtime {out['agentRuntimeArn']}")
    return out


def wait_ready(ctl, runtime_id: str) -> dict:
    t0 = time.time()
    while True:
        got = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = got.get("status")
        print(f"  {time.time() - t0:6.0f}s status={status}")
        if status in ("READY", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            return got
        if time.time() - t0 > 900:
            raise SystemExit(f"runtime still {status} after 15 minutes")
        time.sleep(POLL_SECONDS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from fairbill.config import load_env
    load_env()

    if not a.skip_build:
        subprocess.run([sys.executable, str(REPO / "deploy" / "build_runtime_zip.py")], check=True)
    if not ZIP_PATH.exists():
        raise SystemExit("run deploy/build_runtime_zip.py first")

    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]
    bucket = f"fairbill-runtime-{account}-{REGION}"
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)

    t_start = time.time()
    preflight(sts, s3, iam, ctl, bucket)
    role_arn = ensure_role(iam, account)
    zip_size = upload(s3, bucket)
    out = ensure_runtime(ctl, role_arn, bucket)
    runtime_arn = out["agentRuntimeArn"]
    runtime_id = out.get("agentRuntimeId") or runtime_arn.rsplit("/", 1)[-1]
    got = wait_ready(ctl, runtime_id)
    if got.get("status") != "READY":
        print(json.dumps(got, default=str, indent=2))
        raise SystemExit(f"runtime is {got.get('status')}")

    import zipfile
    unzipped = sum(zi.file_size for zi in zipfile.ZipFile(ZIP_PATH).infolist())
    doc = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "region": REGION, "account": account,
        "runtime_arn": runtime_arn, "runtime_id": runtime_id,
        "runtime_version": out.get("agentRuntimeVersion"),
        "endpoint": f"{runtime_arn}/DEFAULT",
        "role_arn": role_arn,
        "artifact": {"bucket": bucket, "prefix": PREFIX,
                     "zipped_bytes": zip_size, "unzipped_bytes": unzipped},
        "environment": ENV_VARS,
        "deploy_seconds": round(time.time() - t_start, 1),
        "log_group": f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
    }
    if DEPLOY_JSON.exists():
        doc = {**json.loads(DEPLOY_JSON.read_text(encoding="utf-8")), **doc}
    DEPLOY_JSON.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
