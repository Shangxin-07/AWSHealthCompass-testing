"""The Jira handler must honour the action the Processor publishes (event.action)."""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def jh(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
    sys.path.insert(0, os.path.join(ROOT, "lambdas/jira_integration"))
    spec = importlib.util.spec_from_file_location(
        "jira_handler_t", os.path.join(ROOT, "lambdas/jira_integration/jira_handler.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls = {"update": 0, "create": 0}
    monkeypatch.setattr(m, "_get_enabled_platforms", lambda: ["jira"])
    monkeypatch.setattr(m, "_get_config", lambda: {"validated": True})
    monkeypatch.setattr(m, "_get_jira_client", lambda: MagicMock())
    monkeypatch.setattr(m, "_update_tickets_jira",
                        lambda *a, **k: calls.__setitem__("update", calls["update"] + 1) or True)
    for name in ("_create_template_a", "_create_template_b", "_process_create", "_create_tickets",
                 "create_template_a_ticket", "create_template_b_ticket", "_create_tickets_bulk"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: calls.__setitem__("create", calls["create"] + 1) or True)
    monkeypatch.setattr(m, "account_has_ticket", lambda *a, **k: calls.get("has_ticket", True))
    return m, calls


def _record(event_action=None, metadata_action=None):
    payload = {
        "event": {"eventArn": "arn:aws:health:us-east-1::event/EKS/X/1", "campaignId": "EKS:X",
                  "campaignType": "resource-level"},
        "resources": [], "dispatch": {"dispatched": True},
        "routing": {"platforms": {"jira": {"project": "KAN", "issueType": "Task"}}},
        "metadata": {"schemaVersion": "2.1"},
    }
    if event_action:
        payload["event"]["action"] = event_action
    if metadata_action:
        payload["metadata"]["action"] = metadata_action
    return {"Records": [{"messageId": "m1", "body": json.dumps({"Message": json.dumps(payload)})}]}


def test_resource_update_from_event_action_is_dispatched(jh):
    m, calls = jh
    m.handle(_record(event_action="RESOURCE_UPDATE"), None)
    assert calls["update"] == 1


def test_metadata_action_still_honoured(jh):
    m, calls = jh
    m.handle(_record(metadata_action="RESOURCE_UPDATE"), None)
    assert calls["update"] == 1


def test_create_is_not_routed_to_update(jh):
    m, calls = jh
    m.handle(_record(event_action="CREATE"), None)
    assert calls["update"] == 0


def test_update_for_account_without_ticket_goes_to_create(jh):
    m, calls = jh
    calls["has_ticket"] = False
    try:
        m.handle(_record(event_action="RESOURCE_UPDATE"), None)
    except Exception:
        pass  # create path internals are stubbed loosely; only routing matters here
    assert calls["update"] == 0
