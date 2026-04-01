"""
cdk_stack.py
────────────
AWS CDK v2 stack for the Trend Spotter API.

Resources created
─────────────────
  • DynamoDB table (single-table design, PAY_PER_REQUEST, TTL enabled)
  • Lambda function (Python 3.12, bundled with pip)
  • Lambda IAM role with least-privilege permissions
  • API Gateway v1 (REST) with:
      – Lambda proxy integration
      – API Key + Usage Plan (enforced at gateway level)
      – CloudWatch access logging
  • CloudWatch Log Group for Lambda

SSM parameters (NOT managed by CDK — created externally before first deploy)
─────────────────────────────────────────────────────────────────────────────
  /trend-spotter/dataforseo-login     SecureString
  /trend-spotter/dataforseo-password  SecureString
  /trend-spotter/api-key              SecureString

  In GitHub Actions these are written from repo secrets before cdk deploy.
  CloudFormation resolves them via {{resolve:ssm:...}} at deploy time and
  injects them directly into the Lambda environment — the Lambda itself never
  calls SSM at runtime.

Prerequisites (local deploy)
─────────────────────────────
  1. pip install -r requirements-cdk.txt
  2. Write the three SSM params (see README).
  3. cdk bootstrap aws://<ACCOUNT>/<REGION>   (once per account/region)
  4. cdk deploy
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class TrendSpotterStack(Stack):
    """Complete infrastructure for the Trend Spotter API."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── DynamoDB table ────────────────────────────────────────────────────

        table = dynamodb.Table(
            self,
            "TrendSpotterTable",
            table_name="trend-spotter",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,  # keep data on stack destroy
            time_to_live_attribute="ttl",
            point_in_time_recovery=True,
        )

        # ── SSM Parameters (pre-existing, managed outside CDK) ───────────────
        # These parameters must exist in SSM before deploying this stack.
        # In GitHub Actions they are written from repo secrets before cdk deploy.
        # Locally, create them once with:
        #
        #   aws ssm put-parameter --name /trend-spotter/dataforseo-login \
        #     --value "YOUR_LOGIN" --type String --overwrite
        #   aws ssm put-parameter --name /trend-spotter/dataforseo-password \
        #     --value "YOUR_PASS" --type String --overwrite

        dataforseo_login_param = ssm.StringParameter.from_string_parameter_name(
            self, "DataForSEOLogin", "/trend-spotter/dataforseo-login"
        )
        dataforseo_password_param = ssm.StringParameter.from_string_parameter_name(
            self, "DataForSEOPassword", "/trend-spotter/dataforseo-password"
        )
        reddit_client_id_param = ssm.StringParameter.from_string_parameter_name(
            self, "RedditClientId", "/trend-spotter/reddit-client-id"
        )
        reddit_client_secret_param = ssm.StringParameter.from_string_parameter_name(
            self, "RedditClientSecret", "/trend-spotter/reddit-client-secret"
        )

        # ── Lambda IAM role ───────────────────────────────────────────────────

        lambda_role = iam.Role(
            self,
            "TrendSpotterLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # DynamoDB access
        table.grant_read_write_data(lambda_role)

        # SSM read access for DataForSEO and Reddit credentials
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    dataforseo_login_param.parameter_arn,
                    dataforseo_password_param.parameter_arn,
                    reddit_client_id_param.parameter_arn,
                    reddit_client_secret_param.parameter_arn,
                ],
            )
        )

        # ── CloudWatch Log Group ──────────────────────────────────────────────

        log_group = logs.LogGroup(
            self,
            "TrendSpotterLambdaLogs",
            log_group_name="/aws/lambda/trend-spotter",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Lambda function ───────────────────────────────────────────────────
        # CDK bundles the ./app directory + requirements.txt via pip.
        # The bundling runs inside a Docker container matching the Lambda runtime.

        fn = lambda_.Function(
            self,
            "TrendSpotterFunction",
            function_name="trend-spotter",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="app.main.handler",
            code=lambda_.Code.from_asset(
                ".",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        (
                            "pip install -r requirements.txt -t /asset-output --quiet "
                            "&& cp -r app /asset-output/app"
                        ),
                    ],
                ),
            ),
            role=lambda_role,
            memory_size=512,
            timeout=Duration.seconds(60),  # DataForSEO live can be slow
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
                "AWS_ACCOUNT_REGION": self.region,
                # Credentials are read from SSM at startup via Lambda env var injection.
                # We resolve the SSM values at deploy time using CDK:
                "DATAFORSEO_LOGIN": ssm.StringParameter.value_for_string_parameter(
                    self, "/trend-spotter/dataforseo-login"
                ),
                "DATAFORSEO_PASSWORD": ssm.StringParameter.value_for_string_parameter(
                    self, "/trend-spotter/dataforseo-password"
                ),
                "REDDIT_CLIENT_ID": ssm.StringParameter.value_for_string_parameter(
                    self, "/trend-spotter/reddit-client-id"
                ),
                "REDDIT_CLIENT_SECRET": ssm.StringParameter.value_for_string_parameter(
                    self, "/trend-spotter/reddit-client-secret"
                ),
                "AWS_REGION_NAME": self.region,
                "ROOT_PATH": "/prod",
            },
            log_group=log_group,
        )

        # ── API Gateway ───────────────────────────────────────────────────────

        api_log_group = logs.LogGroup(
            self,
            "TrendSpotterApiLogs",
            log_group_name="/aws/apigateway/trend-spotter",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api = apigw.RestApi(
            self,
            "TrendSpotterApi",
            rest_api_name="trend-spotter",
            description="Trend Spotter API – current trending topics by category",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,  # disable full request/response logging (PII risk)
                metrics_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(api_log_group),
                access_log_format=apigw.AccessLogFormat.clf(),
            ),
            # API key required at the API level; enforced per-method below
            api_key_source_type=apigw.ApiKeySourceType.HEADER,
        )

        # Lambda integration (proxy — all request details forwarded to FastAPI)
        lambda_integration = apigw.LambdaIntegration(
            fn,
            proxy=True,
            allow_test_invoke=False,
        )

        # API Gateway API Key + Usage Plan
        gw_api_key = apigw.ApiKey(
            self,
            "TrendSpotterGatewayKey",
            api_key_name="trend-spotter-key",
            description="Internal API key for Trend Spotter",
            enabled=True,
        )

        usage_plan = apigw.UsagePlan(
            self,
            "TrendSpotterUsagePlan",
            name="trend-spotter-plan",
            description="Single-user usage plan",
            throttle=apigw.ThrottleSettings(
                rate_limit=10,    # requests per second
                burst_limit=20,
            ),
            quota=apigw.QuotaSettings(
                limit=1000,
                period=apigw.Period.DAY,
            ),
        )
        usage_plan.add_api_key(gw_api_key)
        usage_plan.add_api_stage(
            api=api,
            stage=api.deployment_stage,
        )

        # Route: catch-all proxy → Lambda
        # FastAPI / Mangum handle the path routing internally.
        root = api.root.add_resource("{proxy+}")
        root.add_method(
            "ANY",
            lambda_integration,
            api_key_required=True,
        )
        # Also handle root path
        api.root.add_method(
            "ANY",
            lambda_integration,
            api_key_required=True,
        )

        # ── Stack outputs ─────────────────────────────────────────────────────

        cdk.CfnOutput(
            self,
            "ApiBaseUrl",
            value=api.url,
            description="API Gateway base URL",
        )
        cdk.CfnOutput(
            self,
            "DynamoTableName",
            value=table.table_name,
            description="DynamoDB table name",
        )
        cdk.CfnOutput(
            self,
            "GatewayApiKeyId",
            value=gw_api_key.key_id,
            description="API Gateway key ID — retrieve value via: aws apigateway get-api-key --api-key <id> --include-value",
        )
