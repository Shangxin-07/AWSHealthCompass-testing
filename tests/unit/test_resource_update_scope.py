"""RESOURCE_UPDATE must touch only the event account's tickets, with per-ticket titles and burndown."""
import importlib.util
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

CID = "ECS:AWS_ECS_PLANNED_LIFECYCLE_EVENT"


@pytest.fixture()
def env():
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        res = r.create_table(TableName="compass-resources", BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"},
                                        {"AttributeName": "trackingKey", "KeyType": "RANGE"}],
                             AttributeDefinitions=[s("campaignId"), s("trackingKey")])
        camps = r.create_table(TableName="compass-campaigns", BillingMode="PAY_PER_REQUEST",
                               KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"}],
                               AttributeDefinitions=[s("campaignId")])
        camps.put_item(Item={"campaignId": CID})
        # Account A (ticket KAN-1): 3 resources, two now resolved. Account B (KAN-2) untouched.
        for acct, tid, statuses in (("610000000001", "KAN-1", ["RESOLVED", "RESOLVED", "PENDING"]),
                                    ("610000000002", "KAN-2", ["PENDING", "PENDING", "PENDING"])):
            for i, h in enumerate(statuses):
                v = f"arn:aws:ecs:us-east-1:{acct}:cluster/c{i}"
                res.put_item(Item={"campaignId": CID, "trackingKey": v, "entityValue": v, "accountId": acct,
                                   "healthStatus": h, "ticketId": tid, "ticketStatus": "Closed"})
        sys.path.insert(0, os.path.join(ROOT, "lambdas/jira_integration"))
        spec = importlib.util.spec_from_file_location(
            "orch_t", os.path.join(ROOT, "lambdas/jira_integration/itsm_orchestrator.py"))
        orch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orch)
        yield orch, res, camps


def _payload(newly):
    return {
        "event": {"campaignId": CID, "campaignType": "resource-level", "service": "ECS",
                  "eventTypeCode": "AWS_ECS_PLANNED_LIFECYCLE_EVENT", "affectedAccount": "610000000001",
                  "action": "RESOURCE_UPDATE", "accountTags": {}},
        "resources": [], "routing": {"accountName": "lt-finance-prod-001"},
        "newlyResolved": newly,
    }


def _run(orch, res, camps, payload):
    updates, comments = [], []
    ok = orch.update_tickets(payload=payload,
                             update_issue_fn=lambda t, f: updates.append((t, f["summary"])),
                             add_comment_fn=lambda t, b: comments.append((t, b)),
                             config={}, resources_table=res, campaigns_table=camps, request_id="r")
    return ok, updates, comments


def test_only_the_event_accounts_ticket_is_updated(env):
    orch, res, camps = env
    ok, updates, comments = _run(orch, res, camps, _payload(
        ["arn:aws:ecs:us-east-1:610000000001:cluster/c0", "arn:aws:ecs:us-east-1:610000000001:cluster/c1"]))
    assert ok
    assert [t for t, _ in updates] == ["KAN-1"]
    title = updates[0][1]
    assert "610000000001 (lt-finance-prod-001)" in title and "(1 pending, 2 resolved)" in title
    assert [t for t, _ in comments] == ["KAN-1"]
    body = str(comments[0][1])
    assert "cluster/c0" in body and "cluster/c1" in body and "cluster/c2" not in body


def test_no_comment_without_newly_resolved_resources(env):
    orch, res, camps = env
    ok, updates, comments = _run(orch, res, camps, _payload([]))
    assert ok and [t for t, _ in updates] == ["KAN-1"] and comments == []


def test_write_resources_reports_newly_resolved(env):
    _, res, camps = env
    from resolve_core.resources import write_resources
    camps.put_item(Item={"campaignId": "C2", "statsVersion": 1, "campaignType": "resource-level"})
    acct = "610000000003"
    ents = lambda st: [{"entityValue": f"arn:aws:ecs:us-east-1:{acct}:cluster/x{i}",
                        "status": st} for i in range(2)]
    first = write_resources(res, camps, "C2", "resource-level", ents("PENDING"), {}, acct, "us-east-1", False, "t")
    assert first.newly_resolved == ()
    second = write_resources(res, camps, "C2", "resource-level", ents("RESOLVED"), {}, acct, "us-east-1", False, "t")
    assert sorted(second.newly_resolved) == [e["entityValue"] for e in ents("RESOLVED")]
    third = write_resources(res, camps, "C2", "resource-level", ents("RESOLVED"), {}, acct, "us-east-1", False, "t")
    assert third.newly_resolved == ()


def test_ticket_write_never_creates_bare_rows(env):
    orch, res, camps = env
    existing = "arn:aws:ecs:us-east-1:610000000001:cluster/c0"
    missing = "arn:aws:lambda:ap-southeast-2:610000000009:function:never-written"
    orch._write_ticket_to_resources(res, CID, [{"trackingKey": existing}, {"arn": missing}],
                                    "KAN-9", "https://x/browse/KAN-9", "now")
    assert res.get_item(Key={"campaignId": CID, "trackingKey": existing})["Item"]["ticketId"] == "KAN-9"
    assert "Item" not in res.get_item(Key={"campaignId": CID, "trackingKey": missing})
    orch._write_back_resource_statuses(res, CID, [{"arn": missing, "status": "RESOLVED"}])
    assert "Item" not in res.get_item(Key={"campaignId": CID, "trackingKey": missing})
