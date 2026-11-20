"""System status and cross-campaign accounts endpoints (moto)."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
sys.path.insert(0, os.path.join(ROOT, "lambdas/api"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def env(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                 "STATS_TABLE": "compass-campaign-stats"}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        for name, keys in (("compass-config", ("pk",)), ("compass-campaigns", ("campaignId",)),
                           ("compass-resources", ("campaignId", "trackingKey")),
                           ("compass-campaign-stats", ("campaignId", "accountId"))):
            ks = [{"AttributeName": keys[0], "KeyType": "HASH"}] + \
                 ([{"AttributeName": keys[1], "KeyType": "RANGE"}] if len(keys) > 1 else [])
            r.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=ks,
                           AttributeDefinitions=[s(k) for k in keys])
        sqs = boto3.client("sqs", region_name="us-east-1")
        for q in ("compass-ingestion", "compass-ingestion-dlq", "compass-jira-integration", "compass-jira-integration-dlq"):
            sqs.create_queue(QueueName=q)
        spec = importlib.util.spec_from_file_location("dashboard_handlers", os.path.join(ROOT, "lambdas/api/dashboard_handlers.py"))
        dh = importlib.util.module_from_spec(spec)
        sys.modules["dashboard_handlers"] = dh
        spec.loader.exec_module(dh)
        dh._get_account_names = lambda ids: {i: f"name-{i[-2:]}" for i in ids}
        spec2 = importlib.util.spec_from_file_location("status_handlers_t", os.path.join(ROOT, "lambdas/api/status_handlers.py"))
        sh = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(sh)
        sh.dh = dh
        yield sh, dh, r, sqs
        sys.modules.pop("dashboard_handlers", None)


def _status(sh):
    out = sh.handle_system_status({}, None)
    assert out["statusCode"] == 200
    return json.loads(out["body"])


def test_never_run_jobs_are_warnings(env):
    sh, *_ = env
    b = _status(sh)
    assert b["overall"] == "warning"
    assert b["sync"]["state"] == b["reconciliation"]["state"] == "warning"
    assert set(b["queues"]) == {"ingestion", "ingestionDlq", "jira", "jiraDlq"}   # ServiceNow not deployed
    assert b["deadLetterMessages"] == 0 and b["alarmsFiring"] == []


def test_failed_sync_and_dlq_messages_are_errors(env):
    sh, dh, r, sqs = env
    cfg = r.Table("compass-config")
    recent = iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    cfg.put_item(Item={"pk": "SYNC_STATE", "updated_at": recent, "last_sync_status": "failed", "errors": 1})
    cfg.put_item(Item={"pk": "RECONCILE_STATE", "status": "completed", "completed_at": recent})
    url = sqs.get_queue_url(QueueName="compass-jira-integration-dlq")["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody="x")
    b = _status(sh)
    assert b["overall"] == "error"
    assert b["sync"]["state"] == "error" and b["reconciliation"]["state"] == "ok"
    assert b["deadLetterMessages"] == 1 and b["queues"]["jiraDlq"]["visible"] == 1


def test_healthy(env):
    sh, dh, r, _ = env
    recent = iso(datetime.now(timezone.utc) - timedelta(minutes=20))
    cfg = r.Table("compass-config")
    cfg.put_item(Item={"pk": "SYNC_STATE", "updated_at": recent, "last_sync_status": "success",
                       "tickets_synced": 411, "errors": 0})
    cfg.put_item(Item={"pk": "RECONCILE_STATE", "status": "completed", "completed_at": recent, "events_found": 3})
    b = _status(sh)
    assert b["overall"] == "ok" and b["problems"] == [] and b["sync"]["ticketsSynced"] == 411


@pytest.mark.parametrize("item,state", [
    ({"updated_at": iso(NOW - timedelta(hours=3)), "last_sync_status": "success"}, "warning"),   # stale
    ({"updated_at": iso(NOW - timedelta(minutes=5)), "last_sync_status": "success", "errors": 2}, "warning"),
    ({"updated_at": iso(NOW - timedelta(minutes=5)), "last_sync_status": "success"}, "ok"),
])
def test_sync_rules(env, item, state):
    sh, *_ = env
    assert sh._sync_status(item, NOW)["state"] == state


@pytest.mark.parametrize("item,state", [
    ({"status": "failed", "completed_at": iso(NOW), "error_details": [{"errorCode": "CONN_HEALTH_API_FAILED"}]}, "warning"),
    ({"status": "completed", "completed_at": iso(NOW - timedelta(hours=30))}, "warning"),
    ({"status": "running", "updated_at": iso(NOW)}, "ok"),
    ({"status": "completed", "completed_at": iso(NOW - timedelta(hours=5))}, "ok"),
])
def test_reconcile_rules(env, item, state):
    sh, *_ = env
    out = sh._reconcile_status(item, NOW)
    assert out["state"] == state
    if item["status"] == "failed":
        assert "CONN_HEALTH_API_FAILED" in out["message"]


def test_accounts_summary_across_campaigns(env):
    sh, dh, r, _ = env
    from resolve_core import campaign_stats as cs
    camps, res, stats = r.Table("compass-campaigns"), r.Table("compass-resources"), r.Table("compass-campaign-stats")
    for cid, status, deadline in (("A", "ACTIVE", "2026-12-01T00:00:00Z"), ("B", "ACTIVE", "2026-11-25T00:00:00Z"),
                                  ("C", "COMPLETED", "2026-11-21T00:00:00Z")):
        camps.put_item(Item={"campaignId": cid, "status": status, "startTime": deadline, "statsVersion": 1})
    rows = [("A", "600000000001", "PENDING"), ("A", "600000000001", "RESOLVED"), ("A", "600000000002", "RESOLVED"),
            ("B", "600000000001", "PENDING"), ("B", "600000000002", "RESOLVED"), ("C", "600000000002", "PENDING")]
    for i, (cid, acct, h) in enumerate(rows):
        res.put_item(Item={"campaignId": cid, "trackingKey": f"r{i}", "accountId": acct, "healthStatus": h,
                           "ticketStatus": "none"})
    for cid in "ABC":
        cs.recount_campaign(res, stats, camps, cid, "now")
    body = json.loads(sh.handle_accounts_summary({"queryStringParameters": {"scope": "active"}}, None)["body"])
    assert body["campaigns"] == 2
    a1, a2 = body["accounts"]
    assert (a1["accountId"], a1["pending"], a1["resolved"], a1["campaigns"], a1["campaignsWithPending"],
            a1["nearestDeadline"], a1["name"]) == ("600000000001", 2, 1, 2, 2, "2026-11-25T00:00:00Z", "name-01")
    assert (a2["accountId"], a2["pending"], a2["resolved"], a2["nearestDeadline"]) == ("600000000002", 0, 2, None)
    allb = json.loads(sh.handle_accounts_summary({"queryStringParameters": {"scope": "all"}}, None)["body"])
    assert {a["accountId"]: a["pending"] for a in allb["accounts"]}["600000000002"] == 1
    assert sh.handle_accounts_summary({"queryStringParameters": {"scope": "bogus"}}, None)["statusCode"] == 400
