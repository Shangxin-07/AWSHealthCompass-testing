"""Named test accounts: registration, generator defaults, and /generate-events wiring."""
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

from resolve_core import test_accounts as ta  # noqa: E402


@pytest.fixture()
def cfg():
    with moto.mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="compass-config", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
        t.put_item(Item={"pk": "ROUTING_DEFAULT", "jira_project": "KAN", "jira_issue_type": "Task"})
        yield t


def test_registers_missing_names_with_default_target_and_keeps_existing(cfg):
    cfg.put_item(Item={"pk": "ROUTING#600000000002", "account_name": "my-own-name", "jira_project": "OPS"})
    created = ta.ensure_registered(cfg, ta.TEST_ACCOUNT_IDS + ["999999999999"], "now")
    assert created == len(ta.TEST_ACCOUNT_IDS) - 1          # existing one and non-catalogue skipped
    one = cfg.get_item(Key={"pk": "ROUTING#600000000001"})["Item"]
    assert (one["account_name"], one["jira_project"]) == ("lt-finance-prod", "KAN")
    two = cfg.get_item(Key={"pk": "ROUTING#600000000002"})["Item"]
    assert (two["account_name"], two["jira_project"]) == ("my-own-name", "OPS")
    assert "Item" not in cfg.get_item(Key={"pk": "ROUTING#999999999999"})
    assert ta.ensure_registered(cfg, ta.TEST_ACCOUNT_IDS, "now") == 0    # idempotent
    listing = ta.list_accounts(cfg)
    assert len(listing) == 10 and all(a["registered"] for a in listing)


def test_generator_defaults_match_catalogue():
    spec = importlib.util.spec_from_file_location("gen_defaults", os.path.join(ROOT, "lambdas/event_generator/handler.py"))
    sys.path.insert(0, os.path.join(ROOT, "lambdas/event_generator"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    assert g.DEFAULT_ACCOUNTS == ta.TEST_ACCOUNT_IDS


def _api(cfg):
    os.environ["EVENT_GENERATOR_FUNCTION_NAME"] = "gen"
    sys.path.insert(0, os.path.join(ROOT, "lambdas/api"))
    spec = importlib.util.spec_from_file_location("dh_ta", os.path.join(ROOT, "lambdas/api/dashboard_handlers.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    sent = []
    m._lambda_client = MagicMock()
    m._lambda_client.invoke.side_effect = lambda **kw: sent.append(json.loads(kw["Payload"])) or {"Payload": io.BytesIO(b"{}")}
    return m, sent


def test_bulk_generation_uses_named_accounts_and_registers_names(cfg):
    m, sent = _api(cfg)
    out = m.handle_generate_events({"body": json.dumps({"count": 10})}, None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 202 and body["accounts"] == 10 and body["namesRegistered"] == 10
    assert sent[0]["accounts"] == ta.TEST_ACCOUNT_IDS


def test_single_event_defaults_to_first_test_account(cfg):
    m, sent = _api(cfg)
    m.handle_generate_events({"body": json.dumps({"singleEvent": {"service": "EKS"}})}, None)
    assert sent[0]["singleEvent"]["accountId"] == "600000000001"
    m.handle_generate_events({"body": json.dumps({"singleEvent": {"service": "EKS", "accountId": "123456789012"}})}, None)
    assert sent[1]["singleEvent"]["accountId"] == "123456789012"
    assert "Item" not in cfg.get_item(Key={"pk": "ROUTING#123456789012"})   # non-catalogue: not registered
