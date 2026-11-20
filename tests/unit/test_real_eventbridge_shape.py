"""Processor with a real-shaped EventBridge Health event (structure of a real event, anonymised).

Real EventBridge events carry the account once (detail.affectedAccount), have no
awsAccountId on affectedEntities, and use detail.eventRegion rather than detail.region.
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "eventbridge_health_lambda_ple.json")
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

CID = "LAMBDA:AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT"
ACCT = "123456789012"


@pytest.fixture()
def run(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        for name, keys in (("compass-resources", ("campaignId", "trackingKey")),
                           ("compass-campaigns", ("campaignId",)),
                           ("compass-config", ("pk",)),
                           ("compass-campaign-stats", ("campaignId", "accountId"))):
            ks = [{"AttributeName": keys[0], "KeyType": "HASH"}] + \
                 ([{"AttributeName": keys[1], "KeyType": "RANGE"}] if len(keys) > 1 else [])
            r.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=ks,
                           AttributeDefinitions=[s(k) for k in keys])
        r.Table("compass-config").put_item(Item={"pk": "ROUTING_DEFAULT", "jira_project": "KAN",
                                                 "jira_issue_type": "Task"})
        sns = boto3.client("sns", region_name="us-east-1")
        sqs = boto3.client("sqs", region_name="us-east-1")
        topic = sns.create_topic(Name="compass-integration")["TopicArn"]
        q = sqs.create_queue(QueueName="tap")["QueueUrl"]
        qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn,
                      Attributes={"RawMessageDelivery": "true"})
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="payloads")
        for k, v in {"CAMPAIGNS_TABLE": "compass-campaigns", "RESOURCES_TABLE": "compass-resources",
                     "CONFIG_TABLE": "compass-config", "STATS_TABLE": "compass-campaign-stats",
                     "INTEGRATION_TOPIC_ARN": topic, "PAYLOAD_BUCKET": "payloads",
                     "AWS_REGION": "us-east-1"}.items():
            monkeypatch.setenv(k, v)
        sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
        spec = importlib.util.spec_from_file_location("proc_real", os.path.join(ROOT, "lambdas/processor/handler.py"))
        proc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proc)
        event = json.load(open(FIXTURE))
        out = proc.lambda_handler({"Records": [{"messageId": "m1", "body": json.dumps(event)}]}, None)
        msgs = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10).get("Messages", [])
        yield out, r, [json.loads(m["Body"]) for m in msgs]


def test_resources_are_tracked_under_the_event_account(run):
    out, r, _ = run
    assert out == {"batchItemFailures": []}
    rows = r.Table("compass-resources").query(
        KeyConditionExpression="campaignId = :c", ExpressionAttributeValues={":c": CID})["Items"]
    assert len(rows) == 2
    assert {x["accountId"] for x in rows} == {ACCT}
    assert {x["healthStatus"] for x in rows} == {"PENDING"}
    assert {x["region"] for x in rows} == {"ap-southeast-2"}
    stats = r.Table("compass-campaign-stats").get_item(Key={"campaignId": CID, "accountId": ACCT})["Item"]
    assert int(stats["total"]) == 2 and int(stats["pending"]) == 2


def test_published_event_carries_account_region_and_tracking_keys(run):
    _, _, published = run
    assert len(published) == 1
    payload = published[0]
    assert payload["event"]["region"] == "ap-southeast-2"
    assert payload["event"]["action"] == "CREATE"
    assert {res["accountId"] for res in payload["resources"]} == {ACCT}
    assert all(res["trackingKey"] == res["entityValue"] for res in payload["resources"])
