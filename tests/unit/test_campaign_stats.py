"""Per-account campaign counters against moto's DynamoDB (real ADD/pagination semantics)."""
import base64
import importlib.util
import json
import os
import random
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))

moto = pytest.importorskip("moto")
import boto3  # noqa: E402

from resolve_core import campaign_stats as cs  # noqa: E402
from resolve_core.resources import write_resources  # noqa: E402

CID = "EKS:AWS_EKS_PLANNED_LIFECYCLE_EVENT"
NOW = "2026-11-20T03:00:00Z"


@pytest.fixture()
def ddb():
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        res = r.create_table(
            TableName="compass-resources", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"},
                       {"AttributeName": "trackingKey", "KeyType": "RANGE"}],
            AttributeDefinitions=[s("campaignId"), s("trackingKey"), s("ticketStatus")],
            GlobalSecondaryIndexes=[{"IndexName": "ticketStatus-index",
                                     "KeySchema": [{"AttributeName": "ticketStatus", "KeyType": "HASH"}],
                                     "Projection": {"ProjectionType": "KEYS_ONLY"}}])
        camps = r.create_table(
            TableName="compass-campaigns", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"}],
            AttributeDefinitions=[s("campaignId"), s("status"), s("updatedAt")],
            GlobalSecondaryIndexes=[{"IndexName": "status-updatedAt-index",
                                     "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                                                   {"AttributeName": "updatedAt", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}}])
        stats = r.create_table(
            TableName="compass-campaign-stats", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"},
                       {"AttributeName": "accountId", "KeyType": "RANGE"}],
            AttributeDefinitions=[s("campaignId"), s("accountId")])
        r.create_table(
            TableName="compass-config", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[s("pk")])
        yield res, camps, stats


def _campaign(camps, cid=CID, stats_version=True, status="ACTIVE"):
    item = {"campaignId": cid, "status": status, "updatedAt": NOW, "campaignType": "resource-level",
            "totalResourceCount": 0, "pendingCount": 0, "resolvedCount": 0}
    if stats_version:
        item["statsVersion"] = cs.STATS_VERSION
    camps.put_item(Item=item)


def _entities(account, n, status="PENDING", start=0):
    return [{"entityValue": f"arn:aws:eks:us-east-1:{account}:cluster/c{start + i}",
             "awsAccountId": account, "status": status} for i in range(n)]


def _write(res, camps, stats, account, ents, is_new=False, cid=CID):
    return write_resources(res, camps, cid, "resource-level", ents, {}, account,
                           "us-east-1", is_new, NOW, stats_table=stats)


def _stats_by_acct(stats, cid=CID):
    return {i["accountId"]: cs.summarize_account(i) for i in cs.read_stats(stats, cid)}


def _camp_totals(camps, cid=CID):
    c = camps.get_item(Key={"campaignId": cid})["Item"]
    return int(c["totalResourceCount"]), int(c["pendingCount"]), int(c["resolvedCount"])


def test_new_campaign_batch_counts_per_account(ddb):
    res, camps, stats = ddb
    _campaign(camps)
    _write(res, camps, stats, "111111111111", _entities("111111111111", 3), is_new=True)
    _write(res, camps, stats, "222222222222", _entities("222222222222", 2))
    by = _stats_by_acct(stats)
    assert by["111111111111"]["total"] == 3 and by["111111111111"]["pending"] == 3
    assert by["222222222222"]["total"] == 2
    assert by["222222222222"]["unticketedUnresolved"] == 2
    assert _camp_totals(camps) == (5, 5, 0)


def test_health_transition_moves_counts(ddb):
    res, camps, stats = ddb
    _campaign(camps)
    acct = "111111111111"
    _write(res, camps, stats, acct, _entities(acct, 3), is_new=True)
    _write(res, camps, stats, acct, _entities(acct, 2, status="RESOLVED"))  # c0, c1 resolve
    by = _stats_by_acct(stats)[acct]
    assert (by["total"], by["pending"], by["resolved"]) == (3, 1, 2)
    assert by["unticketedUnresolved"] == 1
    assert _camp_totals(camps) == (3, 1, 2)


def test_redelivered_event_does_not_double_count(ddb):
    res, camps, stats = ddb
    _campaign(camps)
    acct = "111111111111"
    for _ in range(3):  # same event processed three times
        _write(res, camps, stats, acct, _entities(acct, 4))
    assert _stats_by_acct(stats)[acct]["total"] == 4
    assert _camp_totals(camps) == (4, 4, 0)


def test_pre_stats_campaign_is_recounted_on_first_touch(ddb):
    res, camps, stats = ddb
    _campaign(camps, stats_version=False)
    acct = "111111111111"
    # Rows that existed before stats were tracked:
    for e in _entities(acct, 5):
        res.put_item(Item={"campaignId": CID, "trackingKey": e["entityValue"], "accountId": acct,
                           "healthStatus": "PENDING", "ticketStatus": "none"})
    _write(res, camps, stats, acct, _entities(acct, 1, start=5))
    assert _stats_by_acct(stats)[acct]["total"] == 6
    assert camps.get_item(Key={"campaignId": CID})["Item"]["statsVersion"] == cs.STATS_VERSION


def test_incremental_matches_recount_over_random_history(ddb):
    res, camps, stats = ddb
    _campaign(camps)
    rnd = random.Random(7)
    accounts = [f"{600000000000 + i:012d}" for i in range(6)]
    for step in range(60):
        acct = rnd.choice(accounts)
        ents = [{"entityValue": f"arn:aws:eks:us-east-1:{acct}:cluster/c{rnd.randrange(8)}",
                 "awsAccountId": acct, "status": rnd.choice(["PENDING", "RESOLVED"])}
                for _ in range(rnd.randrange(1, 4))]
        _write(res, camps, stats, acct, ents, is_new=(step == 0))
    incremental = _stats_by_acct(stats)
    inc_totals = _camp_totals(camps)
    cs.recount_campaign(res, stats, camps, CID, NOW)
    assert _stats_by_acct(stats) == incremental
    assert _camp_totals(camps) == inc_totals


def test_recount_tracks_ticket_status_cross_tab_and_removes_stale_accounts(ddb):
    res, camps, stats = ddb
    _campaign(camps)
    rows = [("111111111111", "PENDING", "Created"), ("111111111111", "RESOLVED", "Closed"),
            ("111111111111", "RESOLVED", "none"), ("222222222222", "PENDING", "In Progress")]
    for i, (a, h, t) in enumerate(rows):
        res.put_item(Item={"campaignId": CID, "trackingKey": f"r{i}", "accountId": a,
                           "healthStatus": h, "ticketStatus": t})
    stats.put_item(Item={"campaignId": CID, "accountId": "999999999999", "total": 5})  # stale
    cs.recount_campaign(res, stats, camps, CID, NOW)
    by = _stats_by_acct(stats)
    assert set(by) == {"111111111111", "222222222222"}
    assert by["111111111111"]["ticketStatus"] == {"Created": 1, "Closed": 1, "none": 1}
    assert by["111111111111"]["unticketedUnresolved"] == 0  # its unticketed row is resolved
    groups = cs.ticket_status_groups(cs.read_stats(stats, CID))
    assert groups["Closed"] == {"total": 1, "resolved": 1, "pending": 0}
    assert groups["In Progress"]["pending"] == 1


# --------------------------------------------------------------------------- API


def _load_api():
    os.environ["STATS_TABLE"] = "compass-campaign-stats"
    sys.path.insert(0, os.path.join(ROOT, "lambdas/api"))
    spec = importlib.util.spec_from_file_location(
        "dashboard_handlers_t", os.path.join(ROOT, "lambdas/api/dashboard_handlers.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.operative_platform = lambda *_: "jira"
    m.resolve_platforms = lambda *_: ["jira"]
    m._get_account_names = lambda ids: {i: f"name-{i[-3:]}" for i in ids}
    return m


def _ev(path, cid, qs=None):
    return {"httpMethod": "GET", "resource": path, "pathParameters": {"id": cid},
            "queryStringParameters": qs}


def _seed_big(res, camps, stats, n_accounts=5, per=60):
    _campaign(camps)
    for i in range(n_accounts):
        acct = f"{610000000000 + i:012d}"
        ents = _entities(acct, per)
        for e in ents[: per // 3]:
            e["status"] = "RESOLVED"
        _write(res, camps, stats, acct, ents, is_new=(i == 0))
    return n_accounts * per


def test_api_detail_embeds_first_page_and_stats_breakdown(ddb):
    res, camps, stats = ddb
    total = _seed_big(res, camps, stats)
    api = _load_api()
    body = json.loads(api.handle_campaign_detail(_ev("/api/campaigns/{id}", CID), None)["body"])
    assert len(body["resources"]) == api._DETAIL_RESOURCE_LIMIT and body["resourcesTruncated"] is True
    assert body["totalResources"] == total
    assert sum(g["total"] for g in body["groupBreakdown"].values()) == total
    assert all(g["resolved"] == 20 and g["pending"] == 40 for g in body["groupBreakdown"].values())
    assert body["ticketableAccounts"] == 5


def test_api_resources_pages_through_everything_once(ddb):
    res, camps, stats = ddb
    total = _seed_big(res, camps, stats)
    api = _load_api()
    seen, token = [], None
    while True:
        qs = {"limit": "37"} | ({"nextToken": token} if token else {})
        body = json.loads(api.handle_campaign_resources(_ev("/api/campaigns/{id}/resources", CID, qs), None)["body"])
        seen += [r["resourceArn"] for r in body["resources"]]
        token = body["nextToken"]
        if not token:
            break
    assert len(seen) == total and len(set(seen)) == total
    assert body["totalCount"] == total


def test_api_resources_filters_and_counts(ddb):
    res, camps, stats = ddb
    _seed_big(res, camps, stats)
    api = _load_api()
    acct = "610000000002"
    seen, token = [], None
    while True:
        qs = {"limit": "7", "status": "RESOLVED", "accountId": acct} | ({"nextToken": token} if token else {})
        body = json.loads(api.handle_campaign_resources(_ev("/api/campaigns/{id}/resources", CID, qs), None)["body"])
        seen += body["resources"]
        token = body["nextToken"]
        if not token:
            break
    assert len(seen) == 20 and all(r["accountId"] == acct and r["healthStatus"] == "RESOLVED" for r in seen)
    assert body["filteredCount"] == 20


@pytest.mark.parametrize("token", ["not-base64!!", base64.urlsafe_b64encode(b"5").decode(),
                                   base64.urlsafe_b64encode(json.dumps(
                                       {"campaignId": "OTHER", "trackingKey": "x"}).encode()).decode()])
def test_api_resources_rejects_bad_tokens(ddb, token):
    res, camps, stats = ddb
    _seed_big(res, camps, stats, n_accounts=1, per=3)
    api = _load_api()
    out = api.handle_campaign_resources(_ev("/api/campaigns/{id}/resources", CID, {"nextToken": token}), None)
    assert out["statusCode"] == 400


def test_api_breakdown_by_account_and_ticket_status(ddb):
    res, camps, stats = ddb
    total = _seed_big(res, camps, stats)
    api = _load_api()
    body = json.loads(api.handle_campaign_breakdown(_ev("/api/campaigns/{id}/breakdown", CID), None)["body"])
    assert body["totalResources"] == total and body["resolvedResources"] == 100
    assert body["groups"][0]["groupLabel"].startswith("name-")
    assert all(g["resolved"] == 20 for g in body["groups"])  # previously always 0 (read `status`)
    body = json.loads(api.handle_campaign_breakdown(
        _ev("/api/campaigns/{id}/breakdown", CID, {"groupBy": "ticketStatus"}), None)["body"])
    assert body["groups"] == [{"groupKey": "none", "groupLabel": "No Ticket", "total": total,
                               "pending": 200, "resolved": 100, "completionPct": 33.3,
                               "ticketStatus": {"none": total}}]


# -------------------------------------------------------------------------- Sync


def test_sync_recounts_open_campaigns_only(ddb):
    res, camps, stats = ddb
    _campaign(camps, "OPEN", stats_version=False)
    _campaign(camps, "DONE", stats_version=False, status="COMPLETED")
    for cid in ("OPEN", "DONE"):
        res.put_item(Item={"campaignId": cid, "trackingKey": "r", "accountId": "111111111111",
                           "healthStatus": "PENDING", "ticketStatus": "none"})
    os.environ["STATS_TABLE"] = "compass-campaign-stats"
    spec = importlib.util.spec_from_file_location("sync_t", os.path.join(ROOT, "lambdas/sync/handler.py"))
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    assert sync._recount_open_campaigns(None, NOW) == 1
    assert cs.read_stats(stats, "OPEN") and not cs.read_stats(stats, "DONE")

    class LowTime:
        def get_remaining_time_in_millis(self):
            return 1_000
    assert sync._recount_open_campaigns(LowTime(), NOW) == 0


def test_sync_recount_returns_reopened_campaign_to_completed(ddb):
    """A Health update reopens a campaign (ACTIVE); if all tickets are closed, Sync completes it again."""
    res, camps, stats = ddb
    _campaign(camps, "REOPENED")
    for i in range(3):
        res.put_item(Item={"campaignId": "REOPENED", "trackingKey": f"r{i}", "accountId": "111111111111",
                           "healthStatus": "RESOLVED", "ticketStatus": "Closed", "ticketId": "KAN-1"})
    os.environ["STATS_TABLE"] = "compass-campaign-stats"
    spec = importlib.util.spec_from_file_location("sync_t2", os.path.join(ROOT, "lambdas/sync/handler.py"))
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    sync._recount_open_campaigns(None, NOW)
    item = camps.get_item(Key={"campaignId": "REOPENED"})["Item"]
    assert item["status"] == "COMPLETED"
    assert int(item["ticketsCreated"]) == 1 and int(item["ticketsClosed"]) == 1
