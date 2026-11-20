"""Sync must update every resource row a ticket covers, and aggregate without floats."""
import importlib.util
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def sync():
    sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
    spec = importlib.util.spec_from_file_location("sync_mr", os.path.join(ROOT, "lambdas/sync/handler.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._resources_table = MagicMock()
    m._campaigns_table = MagicMock()
    return m


def _row(tk, status="Created", raw=None):
    return {"campaignId": "C1", "trackingKey": tk, "platform": "jira",
            "ticketStatus": status, "ticketRawStatus": raw}


ISSUE = {"key": "KAN-4", "fields": {"status": {"name": "Resolved", "statusCategory": {"key": "done"}},
                                    "updated": "2026-11-20T01:57:00.000+0000"}}


def test_index_keeps_all_rows_of_a_ticket(sync):
    index = {}
    for tk in ("a", "b", "c"):
        sync._index_item(index, {"campaignId": "C1", "trackingKey": tk, "ticketId": "KAN-4",
                                 "ticketStatus": "Created"})
    assert [e["trackingKey"] for e in index["KAN-4"]] == ["a", "b", "c"]


def test_process_ticket_updates_every_row(sync):
    index = {"KAN-4": [_row("a"), _row("b"), _row("c")]}
    assert sync._process_ticket(ISSUE, index, "2026-11-20T02:00:00Z") == "C1"
    keys = [c.kwargs["Key"]["trackingKey"] for c in sync._resources_table.update_item.call_args_list]
    assert keys == ["a", "b", "c"]


def test_process_ticket_skips_rows_already_current(sync):
    index = {"KAN-4": [_row("a", "Closed", "Resolved"), _row("b")]}
    sync._process_ticket(ISSUE, index, "now")
    keys = [c.kwargs["Key"]["trackingKey"] for c in sync._resources_table.update_item.call_args_list]
    assert keys == ["b"]


def test_aggregate_counts_distinct_tickets_and_uses_decimal(sync):
    rows = ([{"ticketId": "KAN-4", "ticketStatus": "Closed"}] * 3
            + [{"ticketId": "KAN-5", "ticketStatus": "In Progress"}] * 3
            + [{"ticketId": "KAN-6", "ticketStatus": "Created"}] * 2
            + [{"ticketId": "", "ticketStatus": "none"}])
    sync._resources_table.query.return_value = {"Items": rows}
    sync._aggregate_campaign("C1", "now")
    vals = sync._campaigns_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":tc"] == 3 and vals[":tcc"] == 1 and vals[":tip"] == 1
    assert isinstance(vals[":cp"], Decimal) and vals[":cp"] == Decimal("33.3")
