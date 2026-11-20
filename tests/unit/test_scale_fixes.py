"""Unit tests for scale-hardening fixes (no AWS access required)."""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _load(name, rel):
    sys.path.insert(0, os.path.join(ROOT, os.path.dirname(rel)))
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sync():
    return _load("sync_handler", "lambdas/sync/handler.py")


@pytest.fixture(scope="module")
def coverage():
    return _load("coverage_handlers", "lambdas/api/coverage_handlers.py")


def test_ticket_index_uses_gsi_and_batch_get(sync):
    table = MagicMock()
    table.name = "compass-resources"
    pages = {
        "Created": [{"Items": [{"campaignId": "c1", "trackingKey": "a"}],
                     "LastEvaluatedKey": {"k": 1}},
                    {"Items": [{"campaignId": "c1", "trackingKey": "b"}]}],
        "In Progress": [{"Items": [{"campaignId": "c2", "trackingKey": "x"}]}],
        "Closed": [{"Items": []}],
        "unknown": [{"Items": [{"campaignId": "c1", "trackingKey": "a"}]}],  # dup
    }
    def query(**kw):
        assert kw["IndexName"] == "ticketStatus-index"
        return pages[kw["ExpressionAttributeValues"][":s"]].pop(0)
    table.query.side_effect = query
    table.scan.side_effect = AssertionError("must not scan")
    seen_keys = []
    def batch_get(RequestItems):
        keys = RequestItems["compass-resources"]["Keys"]
        seen_keys.extend(keys)
        return {"Responses": {"compass-resources": [
            {"campaignId": k["campaignId"], "trackingKey": k["trackingKey"],
             "ticketId": f"T-{k['trackingKey']}", "ticketStatus": "Created"} for k in keys]}}
    table.meta.client.batch_get_item.side_effect = batch_get
    sync._resources_table = table

    index = sync._build_ticket_index()
    assert set(index) == {"T-a", "T-b", "T-x"}
    assert len(seen_keys) == 3  # deduped
    assert index["T-x"][0]["platform"] == "jira"


def test_batch_get_retries_unprocessed(sync, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    table = MagicMock(); table.name = "t"
    calls = []
    def batch_get(RequestItems):
        calls.append(1)
        if len(calls) == 1:
            return {"Responses": {"t": []}, "UnprocessedKeys": RequestItems}
        return {"Responses": {"t": [{"campaignId": "c", "trackingKey": "k"}]}}
    table.meta.client.batch_get_item.side_effect = batch_get
    sync._resources_table = table
    assert len(sync._batch_get_items([{"campaignId": "c", "trackingKey": "k"}])) == 1
    assert len(calls) == 2


def test_batch_get_gives_up(sync, monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    table = MagicMock(); table.name = "t"
    table.meta.client.batch_get_item.side_effect = lambda RequestItems: {
        "Responses": {}, "UnprocessedKeys": RequestItems}
    sync._resources_table = table
    with pytest.raises(ClientError):
        sync._batch_get_items([{"campaignId": "c", "trackingKey": "k"}])


def test_coverage_scans_all_pages(coverage, monkeypatch):
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"routedVia": "account"}] * 5, "LastEvaluatedKey": {"k": i}}
        for i in range(25)  # > old 10-page cap
    ] + [{"Items": [{"routedVia": "default"}]}]
    monkeypatch.setattr(coverage, "_resources_table", lambda: table)
    import json
    body = json.loads(coverage.handle_routing_coverage({}, None)["body"])
    assert body["totalResources"] == 126
    assert body["partial"] is False


def test_coverage_partial_on_time_budget(coverage, monkeypatch):
    table = MagicMock()
    table.scan.return_value = {"Items": [{"routedVia": "account"}], "LastEvaluatedKey": {"k": 1}}
    monkeypatch.setattr(coverage, "_resources_table", lambda: table)
    monkeypatch.setattr(coverage, "_SCAN_TIME_BUDGET_S", -1)
    import json
    body = json.loads(coverage.handle_routing_coverage({}, None)["body"])
    assert body["partial"] is True and table.scan.call_count == 1
