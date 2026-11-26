"""Alert Approvals (ITSM whitelist) endpoints (moto)."""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
sys.path.insert(0, os.path.join(ROOT, "lambdas/api"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

from resolve_core.dispatch import evaluate_dispatch, load_dispatch_config  # noqa: E402

ADMIN = {"authorizer": {"userId": "admin@example.org", "groups": "Admins"}}
LAMBDA = ("AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT", "scheduledChange")
RDS = ("AWS_RDS_MAINTENANCE_SCHEDULED", "scheduledChange")


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
        spec = importlib.util.spec_from_file_location("dashboard_handlers", os.path.join(ROOT, "lambdas/api/dashboard_handlers.py"))
        dh = importlib.util.module_from_spec(spec)
        sys.modules["dashboard_handlers"] = dh
        spec.loader.exec_module(dh)
        spec2 = importlib.util.spec_from_file_location("alert_handlers_t", os.path.join(ROOT, "lambdas/api/alert_handlers.py"))
        ah = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(ah)
        ah.dh = dh
        yield ah, dh, r
        sys.modules.pop("dashboard_handlers", None)


def _campaign(r, cid, code_cat, accounts, *, dispatched=True, status="ACTIVE", ticketed=()):
    code, cat = code_cat
    r.Table("compass-campaigns").put_item(Item={
        "campaignId": cid, "service": code.split("_")[1], "eventTypeCode": code, "eventTypeCategory": cat,
        "status": status, "dispatched": dispatched, "totalResourceCount": len(accounts),
        "createdAt": "2026-11-20T00:00:00Z", "updatedAt": "2026-11-21T00:00:00Z"})
    for i, a in enumerate(accounts):
        item = {"campaignId": cid, "trackingKey": f"{a}#r{i}", "accountId": a, "healthStatus": "PENDING"}
        if a in ticketed:
            item["ticketStatus"] = "OPEN"
        r.Table("compass-resources").put_item(Item=item)


def call(fn, body=None, ctx=ADMIN):
    out = fn({"body": json.dumps(body) if body is not None else None, "requestContext": ctx}, None)
    return out["statusCode"], json.loads(out["body"])


def body_of(code_cat, **kw):
    return {"eventTypeCode": code_cat[0], "eventTypeCategory": code_cat[1], **kw}


def types_by_code(ah):
    st, b = call(ah.handle_alerts_list)
    assert st == 200
    return b, {t["eventTypeCode"]: t for t in b["types"]}


def test_list_aggregates_by_alert_type(env):
    ah, dh, r = env
    _campaign(r, "c1", LAMBDA, ["111111111111", "222222222222"], ticketed=("222222222222",))
    _campaign(r, "c2", LAMBDA, ["111111111111"], dispatched=False, status="FILTERED")
    _campaign(r, "c3", RDS, ["333333333333"])
    b, t = types_by_code(ah)
    assert b["whitelistEnabled"] is False and b["ruleLimit"] == 100
    lam = t[LAMBDA[0]]
    assert (lam["campaigns"], lam["heldBack"], lam["accounts"], lam["resources"]) == (2, 1, 2, 3)
    assert lam["ticketableAccounts"] == 2          # 111 in c1 and in c2; 222 already ticketed
    assert lam["service"] == "LAMBDA" and lam["approved"] is None


def test_whitelist_toggle_preserves_actionability_filter(env):
    ah, dh, r = env
    cfg = r.Table("compass-config")
    cfg.put_item(Item={"pk": "DISPATCH_PRESET", "mode": "all", "actionability_filter": "none"})
    assert call(ah.handle_alerts_whitelist, {"enabled": True})[0] == 200
    item = cfg.get_item(Key={"pk": "DISPATCH_PRESET"})["Item"]
    assert item["mode"] == "custom" and item["actionability_filter"] == "none"
    assert item["updated_by"] == "admin@example.org"
    assert call(ah.handle_alerts_whitelist, {"enabled": False})[0] == 200
    assert cfg.get_item(Key={"pk": "DISPATCH_PRESET"})["Item"]["mode"] == "all"
    assert call(ah.handle_alerts_whitelist, {"enabled": "yes"})[0] == 400


def test_approve_and_revoke_drive_the_processor_decision(env):
    ah, dh, r = env
    _campaign(r, "c1", LAMBDA, ["111111111111"])
    _campaign(r, "c2", RDS, ["333333333333"])
    call(ah.handle_alerts_whitelist, {"enabled": True})
    cfg = r.Table("compass-config")

    _, t = types_by_code(ah)
    assert t[LAMBDA[0]]["approved"] is False and t[RDS[0]]["approved"] is False

    st, b = call(ah.handle_alerts_approve, body_of(LAMBDA))
    assert st == 200
    # What the Processor loads now dispatches exactly this type and category.
    loaded = load_dispatch_config(cfg)
    assert evaluate_dispatch(*LAMBDA, loaded)["dispatched"] is True
    assert evaluate_dispatch(LAMBDA[0], "accountNotification", loaded)["dispatched"] is False
    assert evaluate_dispatch(*RDS, loaded)["dispatched"] is False

    _, t = types_by_code(ah)
    lam = t[LAMBDA[0]]
    assert lam["approved"] is True and lam["approval"]["approvedBy"] == "admin@example.org"
    assert lam["approvedByOtherRule"] is False

    assert call(ah.handle_alerts_revoke, body_of(LAMBDA))[0] == 200
    assert evaluate_dispatch(*LAMBDA, load_dispatch_config(cfg))["dispatched"] is False
    _, t = types_by_code(ah)
    assert t[LAMBDA[0]]["approved"] is False and t[LAMBDA[0]]["revoked"]["revokedBy"] == "admin@example.org"
    assert call(ah.handle_alerts_revoke, body_of(RDS))[0] == 404


def test_prefix_rule_from_dispatch_window_counts_as_approved(env):
    ah, dh, r = env
    _campaign(r, "c1", LAMBDA, ["111111111111"])
    call(ah.handle_alerts_whitelist, {"enabled": True})
    r.Table("compass-config").put_item(Item={"pk": "DISPATCH_RULE#lambda", "rule_id": "lambda",
        "event_type_pattern": "AWS_LAMBDA_*", "event_categories": ["scheduledChange"], "enabled": True})
    _, t = types_by_code(ah)
    assert t[LAMBDA[0]]["approved"] is True and t[LAMBDA[0]]["approvedByOtherRule"] is True
    assert t[LAMBDA[0]]["approval"] is None


def test_input_validation(env):
    ah, *_ = env
    assert call(ah.handle_alerts_approve, {"eventTypeCode": "lambda", "eventTypeCategory": "scheduledChange"})[0] == 400
    assert call(ah.handle_alerts_approve, {"eventTypeCode": LAMBDA[0], "eventTypeCategory": "issue"})[0] == 400
    assert call(ah.handle_alerts_approve, ["x"])[0] == 400
    # Real codes contain hyphens.
    assert call(ah.handle_alerts_approve, {"eventTypeCode": "AWS_DEVOPS-GURU_X", "eventTypeCategory": "accountNotification"})[0] == 200


def test_rule_limit_counts_enabled_rules_only(env):
    ah, dh, r = env
    cfg = r.Table("compass-config")
    for i in range(100):
        cfg.put_item(Item={"pk": f"DISPATCH_RULE#r{i:03}", "rule_id": f"r{i:03}", "event_type_pattern": f"AWS_X{i}",
                           "event_categories": ["scheduledChange"], "enabled": i < 99})
    assert call(ah.handle_alerts_approve, body_of(LAMBDA))[0] == 200     # 99 enabled + this one
    st, b = call(ah.handle_alerts_approve, body_of(RDS))
    assert st == 400 and b["error"]["code"] == "RULE_LIMIT"
    # Re-approving an existing type is not a new rule.
    assert call(ah.handle_alerts_approve, body_of(LAMBDA))[0] == 200


def test_loader_ignores_disabled_rules_before_the_cap(env):
    ah, dh, r = env
    cfg = r.Table("compass-config")
    cfg.put_item(Item={"pk": "DISPATCH_PRESET", "mode": "custom"})
    for i in range(120):   # revoked rules sort first
        cfg.put_item(Item={"pk": f"DISPATCH_RULE#a{i:03}", "rule_id": f"a{i:03}", "event_type_pattern": "AWS_OLD",
                           "event_categories": ["scheduledChange"], "enabled": False})
    call(ah.handle_alerts_approve, body_of(LAMBDA))
    assert evaluate_dispatch(*LAMBDA, load_dispatch_config(cfg))["dispatched"] is True


def test_send_previews_then_sends_held_back_campaigns(env):
    ah, dh, r = env
    _campaign(r, "c1", LAMBDA, ["111111111111", "222222222222"], dispatched=False, status="FILTERED")
    _campaign(r, "c2", LAMBDA, ["111111111111"], ticketed=("111111111111",))   # nothing to send
    call(ah.handle_alerts_whitelist, {"enabled": True})

    st, b = call(ah.handle_alerts_send, body_of(LAMBDA))
    assert st == 400 and b["error"]["code"] == "NOT_APPROVED"
    call(ah.handle_alerts_approve, body_of(LAMBDA))

    sent = []
    dh.handle_create_tickets = lambda ev, ctx: (sent.append(ev["pathParameters"]["id"]),
                                                {"statusCode": 202, "body": "{}"})[1]
    st, b = call(ah.handle_alerts_send, body_of(LAMBDA))            # dryRun defaults to True
    assert st == 200 and b["dryRun"] is True and (b["campaigns"], b["tickets"]) == (1, 2) and sent == []

    st, b = call(ah.handle_alerts_send, body_of(LAMBDA, dryRun=False))
    assert st == 200 and b["sent"] == 1 and b["failed"] == [] and sent == ["c1"]
    c1 = r.Table("compass-campaigns").get_item(Key={"campaignId": "c1"})["Item"]
    assert c1["dispatched"] is True and c1["status"] == "ACTIVE" and c1["dispatchMode"] == "alert-approvals"


def test_send_reports_failures(env):
    ah, dh, r = env
    _campaign(r, "c1", LAMBDA, ["111111111111"], dispatched=False, status="FILTERED")
    dh.handle_create_tickets = lambda ev, ctx: {"statusCode": 400, "body": json.dumps(
        {"error": {"code": "NO_ITSM", "message": "No ITSM connected."}})}
    st, b = call(ah.handle_alerts_send, body_of(LAMBDA, dryRun=False))   # whitelist off: no approval needed
    assert st == 200 and b["sent"] == 0 and b["failed"][0]["error"] == "No ITSM connected."
    c1 = r.Table("compass-campaigns").get_item(Key={"campaignId": "c1"})["Item"]
    assert c1["status"] == "FILTERED" and c1["dispatched"] is False


def test_routes_and_api_resources_registered():
    src = open(os.path.join(ROOT, "lambdas/api/handler.py")).read()
    for route in ('("GET", "/api/alerts")', '("POST", "/api/alerts/whitelist")', '("POST", "/api/alerts/approve")',
                  '("POST", "/api/alerts/revoke")', '("POST", "/api/alerts/send")'):
        assert route in src
