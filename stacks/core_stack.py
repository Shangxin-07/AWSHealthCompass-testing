"""CompassCore CDK Stack — foundational infrastructure for Compass + ITSM integration.

Creates: 3 Amazon DynamoDB tables, 2 Amazon SQS queues, 1 Amazon SNS topic,
2 Amazon S3 buckets, 1 AWS Secrets Manager secret, 1 AWS Lambda Processor
function (stub), 1 Lambda Layer (empty), 1 Amazon CloudFront distribution
(dashboard), 2 Amazon CloudWatch alarms, cross-region SQS resource policy.

All resources deploy to the primary region (-c primary_region, default us-east-1). This stack blocks every other story.
"""
import re

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_s3 as s3,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
    aws_logs as logs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

from stacks.waf_rules import build_waf_rules


class CoreStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 capture_regions: tuple = ("us-west-2",), **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass core: SQS/SNS/DynamoDB/Processor/S3/CloudFront WAF",
                         **kwargs)

        # ---------------------------------------------------------------
        # Ops Alert Email — required CDK context parameter
        # Fail-fast at synth time: a topic with no subscriber reproduces the
        # exact "alarm fires, nobody told" state this guard exists to close.
        # No placeholder/default is offered by design. Whole-stack synth
        # failure is the approved posture — fail-closed, not fail-open.
        # ---------------------------------------------------------------
        self.ops_alert_email = self.node.try_get_context("ops_alert_email")
        if not self.ops_alert_email:
            raise ValueError(
                "Missing required CDK context parameter 'ops_alert_email'. "
                "CloudWatch alarm notifications require a real subscriber email address. "
                "Deploy with: cdk deploy --all -c account=<ACCOUNT> -c ops_alert_email=<EMAIL>"
            )

        # ---------------------------------------------------------------
        # DynamoDB Tables
        # ---------------------------------------------------------------
        # -c ephemeral=true: test deployments that `cdk destroy` removes
        # completely (data tables, KMS key, all Lambda log groups). Default
        # keeps production-safe RETAIN policies; templates are unchanged.
        ephemeral = self.node.try_get_context("ephemeral") == "true"
        data_removal = RemovalPolicy.DESTROY if ephemeral else RemovalPolicy.RETAIN

        self.config_table = dynamodb.Table(
            self, "ConfigTable",
            table_name="compass-config",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=data_removal,
            time_to_live_attribute="ttl",
        )

        self.campaigns_table = dynamodb.Table(
            self, "CampaignsTable",
            table_name="compass-campaigns",
            partition_key=dynamodb.Attribute(name="campaignId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=data_removal,
        )
        self.campaigns_table.add_global_secondary_index(
            index_name="service-startTime-index",
            partition_key=dynamodb.Attribute(name="service", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="startTime", type=dynamodb.AttributeType.STRING),
        )
        self.campaigns_table.add_global_secondary_index(
            index_name="status-updatedAt-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updatedAt", type=dynamodb.AttributeType.STRING),
        )

        self.resources_table = dynamodb.Table(
            self, "ResourcesTable",
            table_name="compass-resources",
            partition_key=dynamodb.Attribute(name="campaignId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="trackingKey", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=data_removal,
            time_to_live_attribute="expiresAt",
        )
        self.resources_table.add_global_secondary_index(
            index_name="ticketStatus-index",
            partition_key=dynamodb.Attribute(name="ticketStatus", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # Per-campaign, per-account resource counters (resolve_core.campaign_stats).
        # Small items; replaces re-reading every resource row per event/request.
        self.stats_table = dynamodb.Table(
            self, "CampaignStatsTable",
            table_name="compass-campaign-stats",
            partition_key=dynamodb.Attribute(name="campaignId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="accountId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=data_removal,
        )

        # ---------------------------------------------------------------
        # SQS Ingestion Queue + DLQ
        # SQS_MANAGED encryption required.
        # ---------------------------------------------------------------
        self.ingestion_dlq = sqs.Queue(
            self, "IngestionDLQ",
            queue_name="compass-ingestion-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        self.ingestion_queue = sqs.Queue(
            self, "IngestionQueue",
            queue_name="compass-ingestion",
            # Processor Lambda timeout is 15 min; AWS recommends >= 6x so slow runs are not redelivered mid-flight.
            visibility_timeout=Duration.minutes(90),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.ingestion_dlq,
            ),
        )

        # Resource policy: allow the Health capture rule in each capture
        # Region (see -c event_capture in app.py). ArnEquals with the exact
        # rule name + SourceAccount.
        from stacks.event_capture_stack import HEALTH_EVENT_RULE_NAME
        for capture_region in capture_regions:
            self.ingestion_queue.add_to_resource_policy(iam.PolicyStatement(
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[self.ingestion_queue.queue_arn],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": f"arn:aws:events:{capture_region}:{self.account}:rule/{HEALTH_EVENT_RULE_NAME}",
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            ))

        # Allow test EventBridge rule (primary region) when deploy_test_tools is enabled.
        # The test rule uses its own IAM role with sqs:SendMessage, but SQS
        # resource policy must also allow the EventBridge service principal.
        # (Not needed when a regional capture rule already covers the primary Region.)
        if (self.node.try_get_context("deploy_test_tools") == "true"
                and self.region not in capture_regions):
            from stacks.test_tools_stack import TEST_HEALTH_EVENT_RULE_NAME
            self.ingestion_queue.add_to_resource_policy(iam.PolicyStatement(
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[self.ingestion_queue.queue_arn],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": f"arn:aws:events:{self.region}:{self.account}:rule/{TEST_HEALTH_EVENT_RULE_NAME}",
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            ))

        # ---------------------------------------------------------------
        # SNS Integration Topic
        # KMS encryption key required.
        # ---------------------------------------------------------------
        self.sns_key = kms.Key(
            self, "SnsEncryptionKey",
            description="KMS key for Compass SNS topic encryption",
            enable_key_rotation=True,
            # Default (None) keeps CDK's RETAIN for KMS keys.
            removal_policy=RemovalPolicy.DESTROY if ephemeral else None,
            pending_window=Duration.days(7) if ephemeral else None,
        )

        # SQS service principal must decrypt SNS messages
        # for SNS → SQS subscription delivery. Without this, messages silently vanish.
        self.sns_key.grant_decrypt(iam.ServicePrincipal("sqs.amazonaws.com"))

        self.integration_topic = sns.Topic(
            self, "IntegrationTopic",
            topic_name="compass-integration",
            enforce_ssl=True,
            # noqa: CDK API parameter name — not a forbidden term usage
            master_key=self.sns_key,
        )

        # ---------------------------------------------------------------
        # SNS Ops Alerts Topic
        # Dedicated notification target for CloudWatch alarms — separate from
        # self.integration_topic (the business-event fan-out topic, which this
        # change does not touch). Reuse this topic for all future us-east-1
        # alarm actions — do not create a second ops topic.
        #
        # Encryption: AWS-managed alias/aws/sns key, not a dedicated CMK.
        # This topic has no SQS subscriber and is low-volume (alarm state
        # transitions only), so the cross-service decrypt-grant need that
        # justifies self.sns_key for integration_topic does not apply here.
        # ---------------------------------------------------------------
        self.ops_alerts_topic = sns.Topic(
            self, "OpsAlertsTopic",
            topic_name="compass-ops-alerts",
            display_name=f"Compass Operational Alerts ({self.region})",
            enforce_ssl=True,
            master_key=kms.Alias.from_alias_name(self, "OpsAlertsTopicKeyAlias", "alias/aws/sns"),
        )
        self.ops_alerts_topic.add_subscription(
            sns_subs.EmailSubscription(self.ops_alert_email)
        )

        # CDK's add_alarm_action(cw_actions.SnsAction(...)) does NOT automatically
        # grant CloudWatch permission to publish to this topic — verified by
        # CDK source inspection + isolated synth test. Without this
        # explicit statement, alarms detect correctly but notifications are
        # silently denied (reproduces the forbidden "detect but don't
        # notify" state). Do NOT replace this with
        # ops_alerts_topic.grant_publish(iam.ServicePrincipal("cloudwatch.amazonaws.com"))
        # — that helper omits the aws:SourceAccount condition and is a
        # cross-account confused-deputy vulnerability: any AWS
        # account that learns this topic's ARN (e.g. via the CfnOutput below)
        # could point their own CloudWatch alarms at it and publish spoofed
        # notifications. This statement must be added once, immediately after
        # topic creation, before any alarm is wired to it.
        self.ops_alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.ops_alerts_topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        # ---------------------------------------------------------------
        # S3 Payload Offload Bucket
        # KMS_MANAGED encryption.
        # ---------------------------------------------------------------
        self.payload_bucket = s3.Bucket(
            self, "PayloadBucket",
            bucket_name=f"compass-payload-offload-{cdk.Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---------------------------------------------------------------
        # Secrets Manager — JIRA Credentials (empty, populated by onboarding)
        # ---------------------------------------------------------------
        self.jira_secret = secretsmanager.Secret(
            self, "JiraCredentials",
            secret_name="compass/jira-credentials",  # nosec B106 — Secrets Manager resource name, not a credential
            description="JIRA API credentials — populated by onboarding wizard. Do not use placeholder value.",
        )

        # ---------------------------------------------------------------
        # Lambda Layer (empty shell — populated later)
        # ---------------------------------------------------------------
        self.shared_layer = lambda_.LayerVersion(
            self, "CompassCoreLayer",
            code=lambda_.Code.from_asset("lambdas/shared"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Shared resolve_core module",
        )

        # SSM parameter for layer ARN — avoids cross-stack CloudFormation Export
        # which blocks layer updates when consuming stacks import the export.
        ssm.StringParameter(
            self, "SharedLayerArnParam",
            parameter_name="/compass/shared-layer-arn",
            string_value=self.shared_layer.layer_version_arn,
            description="Compass shared Lambda Layer version ARN",
        )

        # ---------------------------------------------------------------
        # Processor Lambda (stub — business logic added later)
        # NO Secrets Manager access, NO secret values in env vars
        # ---------------------------------------------------------------
        # Ephemeral only: a stack-managed log group so destroy removes it.
        # (Not added by default: existing deployments already have a
        # Lambda-created group of this name, and creating it would conflict.)
        processor_log_group = None
        if ephemeral:
            processor_log_group = logs.LogGroup(
                self, "ProcessorLogGroup",
                log_group_name="/aws/lambda/compass-processor",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

        self.processor_fn = lambda_.Function(
            self, "Processor",
            function_name="compass-processor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/processor"),
            layers=[self.shared_layer],
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "CAMPAIGNS_TABLE": self.campaigns_table.table_name,
                "RESOURCES_TABLE": self.resources_table.table_name,
                "STATS_TABLE": self.stats_table.table_name,
                "CONFIG_TABLE": self.config_table.table_name,
                "INTEGRATION_TOPIC_ARN": self.integration_topic.topic_arn,
                "PAYLOAD_BUCKET": self.payload_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
            log_group=processor_log_group,
        )

        # SQS Event Source Mapping
        self.processor_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.ingestion_queue,
                batch_size=1,
                report_batch_item_failures=True,
            )
        )

        # ---------------------------------------------------------------
        # IAM — least privilege via CDK grant_* methods
        # ---------------------------------------------------------------
        self.campaigns_table.grant_read_write_data(self.processor_fn)
        self.resources_table.grant_read_write_data(self.processor_fn)
        self.stats_table.grant_read_write_data(self.processor_fn)
        self.config_table.grant_read_data(self.processor_fn)
        self.integration_topic.grant_publish(self.processor_fn)
        self.sns_key.grant_encrypt_decrypt(self.processor_fn)
        self.payload_bucket.grant_put(self.processor_fn)
        # SQS consume permissions auto-granted by SqsEventSource

        # ---------------------------------------------------------------
        # CloudWatch Alarms
        # ---------------------------------------------------------------
        ingestion_dlq_alarm = cloudwatch.Alarm(
            self, "IngestionDLQAlarm",
            metric=self.ingestion_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Messages in Compass ingestion DLQ — failed event processing",
        )
        # notification action appended, existing alarm
        # block above is unmodified.
        ingestion_dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_alerts_topic))

        processor_error_alarm = cloudwatch.Alarm(
            self, "ProcessorErrorAlarm",
            metric=self.processor_fn.metric_errors(),
            threshold=1,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Compass Processor Lambda sustained errors",
        )
        # notification action appended, existing alarm
        # block above is unmodified.
        processor_error_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_alerts_topic))

        # ---------------------------------------------------------------
        # Dashboard Hosting — S3 + CloudFront
        # ---------------------------------------------------------------
        self.dashboard_bucket = s3.Bucket(
            self, "DashboardBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        oai = cloudfront.OriginAccessIdentity(
            self, "DashboardOAI",
            comment="OAI for Compass dashboard S3 bucket",
        )
        self.dashboard_bucket.grant_read(oai)

        # grant_read alone grants object-level s3:GetObject on bucket/* but NOT
        # bucket-level s3:ListBucket. Without ListBucket, S3 returns 403
        # (Access Denied) for a MISSING key, which the (now-removed) 403->200
        # error response masked as the SPA shell. Granting s3:ListBucket on the
        # BUCKET ARN (not bucket/*) makes S3 return 404 for missing keys, so the
        # retained 404->200 rule serves the SPA deep-link fallback and the
        # WAF-edge 403 reaches the client as a true 403. Stays on OAI (no OAC
        # migration). Principal is the OAI S3 canonical-user (oai.grant_principal).
        self.dashboard_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudFrontOAIListBucket",
                effect=iam.Effect.ALLOW,
                principals=[oai.grant_principal],
                actions=["s3:ListBucket"],
                resources=[self.dashboard_bucket.bucket_arn],
            )
        )

        # ---------------------------------------------------------------
        # WAFv2 — CLOUDFRONT WebACL for the dashboard distribution
        # Created HERE in CoreStack (NOT ApiStack) to avoid a circular
        # stack dependency — the Distribution is a CoreStack resource and
        # ApiStack already depends on CoreStack. Attached at Distribution
        # construction via web_acl_id below. CLOUDFRONT scope requires the
        # WebACL live in us-east-1 (CoreStack is us-east-1).
        # Shared rule set; Option A (AWS-default 403, no custom response).
        # Deploy-time knobs:
        #   -c waf_rate_limit=<n>   default 2000 per-IP / 300s window
        #   -c waf_mode=block|count default 'block' (enforcing).
        # ---------------------------------------------------------------
        waf_mode = self.node.try_get_context("waf_mode") or "block"
        waf_rate_limit = int(self.node.try_get_context("waf_rate_limit") or 2000)

        # CLOUDFRONT-scope WebACLs can only be created in us-east-1. Options:
        #   -c cloudfront_web_acl_arn=<arn>  attach an existing (e.g. org-managed)
        #                                    CLOUDFRONT WebACL; none is created
        #   primary region us-east-1         create the Compass WebACL (default)
        #   any other primary region         no CloudFront WAF (warning at synth);
        #                                    the API's REGIONAL WebACL still applies
        existing_cf_acl = self.node.try_get_context("cloudfront_web_acl_arn")
        if existing_cf_acl and not re.fullmatch(
            r"arn:aws:wafv2:us-east-1:\d{12}:global/webacl/[\w-]+/[\w-]+",
            existing_cf_acl,
        ):
            raise ValueError(
                "cloudfront_web_acl_arn must be a CLOUDFRONT-scope WebACL ARN "
                "(arn:aws:wafv2:us-east-1:<account>:global/webacl/<name>/<id>)"
            )
        create_cf_acl = not existing_cf_acl and self.region == "us-east-1"

        self.cloudfront_acl = None
        if create_cf_acl:
            self.cloudfront_acl = wafv2.CfnWebACL(
                self, "DashboardCloudFrontWebAcl",
                name="compass-dashboard-cloudfront",
                scope="CLOUDFRONT",
                default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name="CompassDashboardCloudFrontAcl",
                    sampled_requests_enabled=True,
                ),
                rules=build_waf_rules(
                    waf_mode=waf_mode,
                    rate_limit=waf_rate_limit,
                ),
            )

        cf_web_acl_id = existing_cf_acl or (
            self.cloudfront_acl.attr_arn if self.cloudfront_acl else None
        )
        if not cf_web_acl_id:
            cdk.Annotations.of(self).add_warning(
                f"No CloudFront WAF on the dashboard: CLOUDFRONT WebACLs must be "
                f"created in us-east-1 and the primary region is {self.region}. "
                f"Pass -c cloudfront_web_acl_arn=<arn> to attach an existing one."
            )

        # SPA routing: rewrite extension-less paths (app routes) to /index.html
        # at the edge. Replaces a distribution-wide 404->200 /index.html error
        # mapping, which also answered missing /assets/*.js with HTML + 200; a
        # browser then refuses the "script" and the page renders blank (seen
        # mid-deploy). Missing files now return a real 404.
        spa_rewrite = cloudfront.Function(
            self, "DashboardSpaRewrite",
            function_name="compass-dashboard-spa-rewrite",
            comment="Rewrite extension-less paths to /index.html",
            code=cloudfront.FunctionCode.from_file(file_path="stacks/cloudfront/spa_rewrite.js"),
        )

        self.dashboard_distribution = cloudfront.Distribution(
            self, "DashboardDistribution",
            web_acl_id=cf_web_acl_id,  # full CLOUDFRONT WebACL ARN, or None
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(
                    self.dashboard_bucket,
                    origin_access_identity=oai,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[cloudfront.FunctionAssociation(
                    function=spa_rewrite,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )],
            ),
            default_root_object="index.html",
            # No error_responses: SPA deep links are handled by spa_rewrite above.
            # (A 403->200 remap was removed earlier because it masked WAF blocks;
            # the 404->200 remap is gone because it masked missing assets.)
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        if create_cf_acl:
            # ---------------------------------------------------------------
            # WAF logging for the CloudFront WebACL.
            # Name MUST start with 'aws-waf-logs-'. Credential headers redacted.
            # ---------------------------------------------------------------
            cf_waf_log_group = logs.LogGroup(
                self, "CloudFrontWafLogGroup",
                log_group_name="aws-waf-logs-compass-cloudfront",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

            # CfnLoggingConfiguration does NOT create
            # the CloudWatch Logs resource policy authorizing WAF's vended-log
            # delivery principal. Without it, delivery is silently denied.
            # Provision it explicitly, scoped to this account + region.
            cf_waf_log_policy = logs.ResourcePolicy(
                self, "CloudFrontWafLogResourcePolicy",
                resource_policy_name="compass-waf-cloudfront-log-delivery",
                policy_statements=[
                    iam.PolicyStatement(
                        sid="AWSWAFLogDeliveryCloudFront",
                        effect=iam.Effect.ALLOW,
                        principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
                        actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                        resources=[cf_waf_log_group.log_group_arn],
                        conditions={
                            "StringEquals": {"aws:SourceAccount": self.account},
                            "ArnLike": {
                                "aws:SourceArn": f"arn:{self.partition}:logs:{self.region}:{self.account}:*",
                            },
                        },
                    ),
                ],
            )

            # LoggingConfiguration destination requires the log-group ARN WITHOUT
            # the trailing ':*' (CDK log_group_arn appends it; WAF rejects it).
            cf_waf_log_group_arn = (
                f"arn:{self.partition}:logs:{self.region}:{self.account}"
                ":log-group:aws-waf-logs-compass-cloudfront"
            )
            cf_waf_logging = wafv2.CfnLoggingConfiguration(
                self, "CloudFrontWafLoggingConfig",
                resource_arn=self.cloudfront_acl.attr_arn,
                log_destination_configs=[cf_waf_log_group_arn],
                redacted_fields=[
                    wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                        single_header={"Name": "authorization"}),
                    wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                        single_header={"Name": "x-api-key"}),
                ],
            )
            cf_waf_logging.node.add_dependency(cf_waf_log_policy)
            cf_waf_logging.node.add_dependency(cf_waf_log_group)

        # BUG-S23-001: Force re-upload every deploy. CDK asset hashing can
        # cache the fingerprint and skip upload even when dist/ content changed.
        # A deploy-time timestamp as custom hash guarantees CloudFormation always
        # sees a new asset and triggers the BucketDeployment custom resource.
        import time as _time

        # Two uploads, in order: hashed assets first (immutable, long-cached),
        # then index.html (no-cache). index.html therefore never references a
        # bundle that hasn't been uploaded yet, and browsers always revalidate it.
        deploy_hash = str(int(_time.time()))
        assets_deployment = s3_deployment.BucketDeployment(
            self, "DashboardDeployment",
            sources=[s3_deployment.Source.asset(
                "./dashboard/dist",
                asset_hash=deploy_hash,
                asset_hash_type=cdk.AssetHashType.CUSTOM,
            )],
            exclude=["index.html"],
            cache_control=[s3_deployment.CacheControl.from_string(
                "public, max-age=31536000, immutable")],
            destination_bucket=self.dashboard_bucket,
            prune=False,  # False: ApiStack also writes config.json to this bucket
        )
        index_deployment = s3_deployment.BucketDeployment(
            self, "DashboardIndexDeployment",
            sources=[s3_deployment.Source.asset(
                "./dashboard/dist",
                asset_hash=deploy_hash + "-index",
                asset_hash_type=cdk.AssetHashType.CUSTOM,
            )],
            exclude=["*"],
            include=["index.html"],
            cache_control=[s3_deployment.CacheControl.no_cache()],
            destination_bucket=self.dashboard_bucket,
            distribution=self.dashboard_distribution,
            distribution_paths=["/index.html", "/"],
            prune=False,
        )
        index_deployment.node.add_dependency(assets_deployment)

        # ---------------------------------------------------------------
        # CDK Outputs for cross-stack / post-deploy reference
        # ---------------------------------------------------------------
        CfnOutput(self, "DashboardUrl",
            value=f"https://{self.dashboard_distribution.distribution_domain_name}",
            description="Dashboard URL (CloudFront)",
        )
        CfnOutput(self, "DashboardBucketName",
            value=self.dashboard_bucket.bucket_name,
            description="Dashboard S3 bucket for manual asset uploads",
        )
        CfnOutput(self, "IngestionQueueArn", value=self.ingestion_queue.queue_arn)
        CfnOutput(self, "IngestionQueueUrl", value=self.ingestion_queue.queue_url)
        CfnOutput(self, "IntegrationTopicArn", value=self.integration_topic.topic_arn)
        CfnOutput(self, "OpsAlertsTopicArn", value=self.ops_alerts_topic.topic_arn)
        CfnOutput(self, "PayloadBucketName", value=self.payload_bucket.bucket_name)
        CfnOutput(self, "JiraSecretArn", value=self.jira_secret.secret_arn)
