"""Resource Lifecycle test controls: API endpoint (moto) and generator replay mode."""
import importlib.util
import io
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

CID = "EKS:AWS_EKS_PLANNED_LIFECYCLE_EVENT"
ARN = "arn:aws:health:us-east-1::event/EKS/AWS_EKS_PLANNED_LIFECYCLE_EVENT/x1"


def _load(name, rel):
    sys.path.insert(0, os.path.join(ROOT, os.path.dirname(rel)))
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def api():
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
        camps.put_item(Item={"campaignId": CID, "campaignType": "resource-level", "service": "EKS",
                             "eventTypeCode": "AWS_EKS_PLANNED_LIFECYCLE_EVENT", "eventArn": ARN})
        camps.put_item(Item={"campaignId": "ACCT", "campaignType": "account-level"})
        for acct, n in (("111111111111", 450), ("222222222222", 3)):
            for i in range(n):
                v = f"arn:aws:eks:ap-southeast-2:{acct}:cluster/c{i}"
                res.put_item(Item={"campaignId": CID, "trackingKey": v, "entityValue": v, "accountId": acct,
                                   "healthStatus": "RESOLVED" if i == 0 else "PENDING", "region": "ap-southeast-2"})
        os.environ["EVENT_GENERATOR_FUNCTION_NAME"] = "gen"
        m = _load("dh_lifecycle", "lambdas/api/dashboard_handlers.py")
        calls = []

        def invoke(FunctionName, InvocationType, Payload):
            batch = json.loads(Payload)["replay"]
            calls.append(batch)
            return {"Payload": io.BytesIO(json.dumps({"statusCode": 200, "published": len(batch)}).encode())}
        m._lambda_client = MagicMock()
        m._lambda_client.invoke.side_effect = invoke
        yield m, calls


def _post(m, body):
    out = m.handle_test_resource_status({"body": json.dumps(body)}, None)
    return out["statusCode"], json.loads(out["body"])


def test_resolve_account_sends_changed_rows_in_pages(api):
    m, calls = api
    code, body = _post(m, {"campaignId": CID, "status": "RESOLVED", "accountId": "111111111111"})
    assert code == 202
    assert body == {"selected": 450, "changed": 449, "eventsSent": 3, "accounts": 1}
    specs = [s for b in calls for s in b]
    assert [len(s["entities"]) for s in specs] == [200, 200, 49]
    s0 = specs[0]
    assert (s0["service"], s0["eventTypeCode"], s0["eventArn"], s0["affectedAccount"], s0["region"]) == \
        ("EKS", "AWS_EKS_PLANNED_LIFECYCLE_EVENT", ARN, "111111111111", "ap-southeast-2")
    assert all(e["status"] == "RESOLVED" for s in specs for e in s["entities"])


def test_selected_resources_only(api):
    m, calls = api
    arns = [f"arn:aws:eks:ap-southeast-2:222222222222:cluster/c{i}" for i in (0, 1)]
    code, body = _post(m, {"campaignId": CID, "status": "PENDING", "resourceArns": arns})
    assert code == 202 and body["selected"] == 2 and body["changed"] == 1  # c1 already pending
    assert calls[0][0]["entities"] == [{"entityValue": arns[0], "status": "PENDING"}]


def test_nothing_to_change_sends_nothing(api):
    m, calls = api
    code, body = _post(m, {"campaignId": CID, "status": "PENDING", "accountId": "222222222222",
                           "resourceArns": ["arn:aws:eks:ap-southeast-2:222222222222:cluster/c2"]})
    assert code == 200 and body["changed"] == 0 and not calls


@pytest.mark.parametrize("body,code", [
    ({"campaignId": CID, "status": "FIXED", "accountId": "111111111111"}, 400),
    ({"campaignId": CID, "status": "RESOLVED"}, 400),                       # no scope
    ({"campaignId": CID, "status": "RESOLVED", "accountId": "12"}, 400),
    ({"campaignId": CID, "status": "RESOLVED", "resourceArns": []}, 400),
    ({"campaignId": "NOPE", "status": "RESOLVED", "accountId": "111111111111"}, 404),
    ({"campaignId": "ACCT", "status": "RESOLVED", "accountId": "111111111111"}, 400),
])
def test_validation(api, body, code):
    m, calls = api
    assert _post(m, body)[0] == code and not calls


def test_requires_test_tools(api, monkeypatch):
    m, _ = api
    monkeypatch.delenv("EVENT_GENERATOR_FUNCTION_NAME")
    assert _post(m, {"campaignId": CID, "status": "RESOLVED", "accountId": "111111111111"})[0] == 400


# ---------------------------------------------------------------- generator replay

@pytest.fixture()
def gen(monkeypatch):
    os.environ["INGESTION_QUEUE_URL"] = "https://sqs/q"
    g = _load("gen_replay", "lambdas/event_generator/handler.py")
    sqs = MagicMock()
    monkeypatch.setattr(g.boto3, "client", lambda *_a, **_k: sqs)
    return g, sqs


SPEC = {"service": "EKS", "eventTypeCode": "AWS_EKS_PLANNED_LIFECYCLE_EVENT", "eventArn": ARN,
        "affectedAccount": "111111111111", "region": "ap-southeast-2",
        "entities": [{"entityValue": "arn:aws:eks:ap-southeast-2:111111111111:cluster/c0", "status": "RESOLVED"}]}


def test_replay_builds_event_for_same_campaign(gen):
    g, sqs = gen
    out = g.lambda_handler({"replay": [SPEC]}, None)
    assert out["statusCode"] == 200 and out["published"] == 1 and out["entities"] == 1
    ev = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    d = ev["detail"]
    assert d["eventArn"] == ARN and d["service"] == "EKS" and d["affectedAccount"] == "111111111111"
    assert d["affectedEntities"][0]["entityValue"] == SPEC["entities"][0]["entityValue"]
    assert d["affectedEntities"][0]["status"] == "RESOLVED"
    from resolve_core.campaign import derive_campaign_id
    assert derive_campaign_id(d) == CID


@pytest.mark.parametrize("bad", [
    {"replay": []},
    {"replay": [{**SPEC, "affectedAccount": "x"}]},
    {"replay": [{**SPEC, "eventArn": "arn:aws:s3:::nope"}]},
    {"replay": [{**SPEC, "entities": [{"entityValue": "a", "status": "BROKEN"}]}]},
    {"replay": [{**SPEC, "entities": [{"entityValue": "a", "status": "PENDING"}] * 1001}]},
])
def test_replay_validation(gen, bad):
    g, sqs = gen
    assert g.lambda_handler(bad, None)["statusCode"] == 400 and not sqs.send_message.called
