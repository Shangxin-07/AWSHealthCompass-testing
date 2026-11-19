#!/usr/bin/env python3
"""Compass + ITSM Integration — CDK App Entry Point."""
import re

import aws_cdk as cdk

from stacks.core_stack import CoreStack
from stacks.event_capture_stack import EventCaptureStack
from stacks.jira_integration_stack import JiraIntegrationStack
from stacks.servicenow_integration_stack import ServiceNowIntegrationStack
from stacks.api_stack import ApiStack
from stacks.test_tools_stack import TestToolsStack

app = cdk.App()

# Primary region for all processing infrastructure. Defaults to us-east-1
# (the AWS Health Organizational View API's home region). Override with
# -c primary_region=<region> where org policy restricts us-east-1; the
# Reconciliation Lambda still calls the Health API's us-east-1 endpoint.
primary_region = app.node.try_get_context("primary_region") or "us-east-1"
if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", primary_region):
    raise ValueError(f"Invalid primary_region context value: {primary_region!r}")

# The us-west-2 Health capture stack is on by default. -c deploy_event_capture=false
# skips it (e.g. for load tests, or where org policy blocks us-west-2); events
# then arrive only via reconciliation or the test tools.
deploy_event_capture = app.node.try_get_context("deploy_event_capture") != "false"

env_primary = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=primary_region,
)
env_west = cdk.Environment(
    account=app.node.try_get_context("account"),
    region="us-west-2",
)

core = CoreStack(app, "CompassCore", env=env_primary)

if deploy_event_capture:
    EventCaptureStack(app, "CompassEventCapture",
        env=env_west,
        ingestion_queue_arn=core.ingestion_queue.queue_arn,
        ops_alert_email=core.ops_alert_email,
    )

jira = JiraIntegrationStack(app, "CompassJira",
    env=env_primary,
    integration_topic=core.integration_topic,
    ops_alerts_topic=core.ops_alerts_topic,
    campaigns_table=core.campaigns_table,
    resources_table=core.resources_table,
    config_table=core.config_table,
    jira_secret=core.jira_secret,
    payload_bucket=core.payload_bucket,
)

# CTRL-02: ServiceNow stack MUST NOT deploy without explicit opt-in.
servicenow = None
if app.node.try_get_context("deploy_servicenow") == "true":
    servicenow = ServiceNowIntegrationStack(app, "CompassServiceNow",
        env=env_primary,
        integration_topic=core.integration_topic,
        ops_alerts_topic=core.ops_alerts_topic,
        campaigns_table=core.campaigns_table,
        resources_table=core.resources_table,
        config_table=core.config_table,
        payload_bucket=core.payload_bucket,
    )

# CTRL-01: Strict string comparison — "true" only, not truthy.
# CTRL-10: Test tools MUST NOT deploy without explicit opt-in.
test_tools = None
if app.node.try_get_context("deploy_test_tools") == "true":
    test_tools = TestToolsStack(app, "CompassTestTools",
        env=env_primary,
        ingestion_queue_arn=core.ingestion_queue.queue_arn,
        ingestion_queue_url=core.ingestion_queue.queue_url,
    )

ApiStack(app, "CompassApi",
    env=env_primary,
    campaigns_table=core.campaigns_table,
    resources_table=core.resources_table,
    config_table=core.config_table,
    jira_secret=core.jira_secret,
    integration_topic=core.integration_topic,
    payload_bucket=core.payload_bucket,
    sns_key=core.sns_key,
    ingestion_queue=core.ingestion_queue,
    jira_fn=jira.jira_lambda,
    dashboard_bucket=core.dashboard_bucket,
    dashboard_distribution=core.dashboard_distribution,
    event_generator_fn=test_tools.generator_fn if test_tools else None,
    servicenow_secret=servicenow.servicenow_secret if servicenow else None,
)

cdk.Tags.of(app).add("auto-delete", "never")

app.synth()
