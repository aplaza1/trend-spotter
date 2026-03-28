"""
cdk_app.py
──────────
Root CDK application entry point.

Usage:
    cdk synth   # preview CloudFormation template
    cdk deploy  # deploy to AWS

Set your target account and region via environment variables:
    export CDK_DEFAULT_ACCOUNT=123456789012
    export CDK_DEFAULT_REGION=us-east-1

Or hardcode them below (not recommended for shared repos).
"""

import aws_cdk as cdk

from cdk_stack import TrendSpotterStack

app = cdk.App()

TrendSpotterStack(
    app,
    "TrendSpotterStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or "us-east-1",
    ),
    description="Trend Spotter API – serverless FastAPI on Lambda + API Gateway + DynamoDB",
)

app.synth()
