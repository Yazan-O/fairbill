"""Kills: 'the hackathon user can deploy now'. One live list call per service (no state change);
the IAM policy simulator is unusable for this user (implicitDeny with missing context even for
calls that succeed live). Exit 0 only when every service answers.

    python deploy/preflight.py
"""
import re
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
REGION = "us-east-1"
CHECKS = [
    ("s3 list", "s3", lambda c: c.list_buckets()),
    ("lambda list", "lambda", lambda c: c.list_functions(MaxItems=1)),
    ("cloudfront list", "cloudfront", lambda c: c.list_distributions(MaxItems="1")),
    ("agentcore list", "bedrock-agentcore-control", lambda c: c.list_agent_runtimes(maxResults=1)),
    ("iam list", "iam", lambda c: c.list_roles(MaxItems=1)),
]


def mask(s: str) -> str:
    return re.sub(r"\d{12}", "2807********", str(s))


def main() -> int:
    ok = True
    for name, svc, call in CHECKS:
        try:
            call(boto3.client(svc, region_name=REGION))
            print(f"{name:16s} OK")
        except ClientError as e:
            ok = False
            print(f"{name:16s} DENIED  {mask(e.response['Error']['Code'])}")
    print("ALL CLEAR (s3:CreateBucket/PutObject are proven only by deploy_runtime.py's first step)"
          if ok else "BLOCKED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
