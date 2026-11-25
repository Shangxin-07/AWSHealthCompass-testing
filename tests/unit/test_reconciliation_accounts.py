"""Reconciliation processes organizational Health events per affected account.

The fake Health client mirrors the real API: entity filters without an
awsAccountId are rejected in failedSet (InvalidAccountInputError), as seen
against the production organization.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

ARN = "arn:aws:health:ap-southeast-2::event/RDS/AWS_RDS_PLANNED_LIFECYCLE_EVENT/AWS_RDS_PLANNED_LIFECYCLE_EVENT_abc"
EVENT = {"arn": ARN, "service": "RDS", "eventTypeCode": "AWS_RDS_PLANNED_LIFECYCLE_EVENT",
         "eventTypeCategory": "scheduledChange", "region": "ap-southeast-2",
         "startTime": datetime(2026, 12, 31, tzinfo=timezone.utc), "statusCode": "upcoming",
         "lastUpdatedTime": datetime(2026, 11, 25, tzinfo=timezone.utc)}


class FakeHealth:
    def __init__(self, entities_by_account):
        self.entities = entities_by_account
        self.entity_calls = []

    def describe_affected_accounts_for_organization(self, eventArn, nextToken=None):
        accts = sorted(self.entities)
        if nextToken is None and len(accts) > 2:           # exercise pagination
            return {"affectedAccounts": accts[:2], "nextToken": "p2"}
        return {"affectedAccounts": accts[2:] if nextToken else accts}

    def get_paginator(self, name):
        assert name == "describe_affected_entities_for_organization"
        fake = self

        class P:
            def paginate(self, organizationEntityFilters):
                fake.entity_calls.append(organizationEntityFilters)
                assert len(organizationEntityFilters) <= 10
                ents, failed = [], []
                for f in organizationEntityFilters:
                    if "awsAccountId" not in f:
                        failed.append({"eventArn": f["eventArn"], "errorName": "InvalidAccountInputError"})
                        continue
                    ents += [{"eventArn": f["eventArn"], "awsAccountId": f["awsAccountId"], "entityValue": v,
                              "entityArn": "x", "lastUpdatedTime": datetime(2026, 11, 25, tzinfo=timezone.utc),
                              "statusCode": "PENDING"}
                             for v in fake.entities.get(f["awsAccountId"], [])]
                return [{"entities": ents, "failedSet": failed}]
        return P()

    def describe_event_details_for_organization(self, organizationEventDetailFilters):
        return {"successfulSet": [{"eventDescription": {"latestDescription": "RDS upgrade needed"}}]}


@pytest.fixture()
def env(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                 "AWS_REGION": "us-east-1", "STATS_TABLE": "compass-campaign-stats"}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        for name, keys in (("compass-resources", ("campaignId", "trackingKey")), ("compass-campaigns", ("campaignId",)),
                           ("compass-config", ("pk",)), ("compass-campaign-stats", ("campaignId", "accountId"))):
            ks = [{"AttributeName": keys[0], "KeyType": "HASH"}] + \
                 ([{"AttributeName": keys[1], "KeyType": "RANGE"}] if len(keys) > 1 else [])
            r.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=ks,
                           AttributeDefinitions=[s(k) for k in keys])
        r.Table("compass-config").put_item(Item={"pk": "ROUTING_DEFAULT", "jira_project": "KAN", "jira_issue_type": "Task"})
        sns, sqs = boto3.client("sns", region_name="us-east-1"), boto3.client("sqs", region_name="us-east-1")
        topic = sns.create_topic(Name="t")["TopicArn"]
        q = sqs.create_queue(QueueName="tap")["QueueUrl"]
        qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn, Attributes={"RawMessageDelivery": "true"})
        monkeypatch.setenv("INTEGRATION_TOPIC_ARN", topic)
        spec = importlib.util.spec_from_file_location("recon_accts", os.path.join(ROOT, "lambdas/reconciliation/handler.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        yield m, r, sqs, q


def _published(sqs, q):
    msgs = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(x["Body"]) for x in msgs]


def test_resources_tracked_per_affected_account(env):
    m, r, sqs, q = env
    m._health_client = FakeHealth({"600000000001": ["arn:aws:rds:ap-southeast-2:600000000001:db:a"],
                                   "600000000002": ["arn:aws:rds:ap-southeast-2:600000000002:db:b",
                                                    "arn:aws:rds:ap-southeast-2:600000000002:db:c"]})
    assert m._process_single_event(EVENT, m._load_config(), "2026-11-25T00:00:00Z") == "ingested"
    rows = r.Table("compass-resources").scan()["Items"]
    by = {}
    for x in rows:
        by.setdefault(x["accountId"], []).append(x["entityValue"])
    assert {k: len(v) for k, v in by.items()} == {"600000000001": 1, "600000000002": 2}
    # Every entity call carried an account ID (the API rejects eventArn-only filters).
    assert all("awsAccountId" in f for call in m._health_client.entity_calls for f in call)
    # One published event per account: the first creates the campaign, the second adds an account.
    pubs = _published(sqs, q)
    assert sorted(p["event"]["affectedAccount"] for p in pubs) == ["600000000001", "600000000002"]


def test_account_level_event_records_each_account(env):
    m, r, *_ = env
    m._health_client = FakeHealth({"600000000003": [], "600000000004": []})
    m._process_single_event({**EVENT, "arn": ARN.replace("RDS", "ACCOUNT", 1)}, m._load_config(), "2026-11-25T00:00:00Z")
    keys = sorted(x["trackingKey"] for x in r.Table("compass-resources").scan()["Items"])
    assert keys == ["ACCOUNT#600000000003", "ACCOUNT#600000000004"]


def test_entity_filters_batched_by_ten(env):
    m, *_ = env
    m._health_client = FakeHealth({f"6000000000{i:02d}": [] for i in range(1, 26)})
    by = m._list_entities_for_accounts(ARN, sorted(m._health_client.entities))
    assert len(by) == 25 and [len(c) for c in m._health_client.entity_calls] == [10, 10, 5]


def test_hyphenated_service_accepted(env):
    m, *_ = env
    assert m._validate_service("DEVOPS-GURU") == "DEVOPS-GURU"
    assert m._validate_service("bad service!") is None


def test_no_affected_accounts_is_skipped(env):
    m, *_ = env
    m._health_client = FakeHealth({})
    assert m._process_single_event(EVENT, m._load_config(), "now") == "skipped"
