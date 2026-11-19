"""update_routed_via must only touch the affected account's rows."""
import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))

from resolve_core.resources import update_routed_via  # noqa: E402


class FakeTable:
    """Partition of rows for one campaign; honours the accountId filter."""
    def __init__(self, rows):
        self.rows = rows
        self.updated = []
        self.queries = []

    def query(self, **kw):
        self.queries.append(kw)
        acct = kw["ExpressionAttributeValues"].get(":acct")
        if "FilterExpression" in kw:
            assert kw["FilterExpression"] == "accountId = :acct"
        items = [r for r in self.rows if acct is None or r["accountId"] == acct]
        return {"Items": [{"campaignId": r["campaignId"], "trackingKey": r["trackingKey"]} for r in items]}

    def update_item(self, **kw):
        self.updated.append(kw["Key"]["trackingKey"])


def _rows(accounts, per=3):
    return [{"campaignId": "EKS:PLE", "trackingKey": f"{a}-{i}", "accountId": a}
            for a in accounts for i in range(per)]


def test_scoped_to_account():
    t = FakeTable(_rows(["111111111111", "222222222222", "333333333333"]))
    update_routed_via(t, "EKS:PLE", "account", None, "now", account_id="222222222222")
    assert sorted(t.updated) == ["222222222222-0", "222222222222-1", "222222222222-2"]


def test_org_wide_cost_is_linear():
    accounts = [f"{500000000000 + i:012d}" for i in range(50)]
    t = FakeTable(_rows(accounts))
    for a in accounts:
        update_routed_via(t, "EKS:PLE", "error", "x", "now", account_id=a)
    assert len(t.updated) == 50 * 3  # previously 50 * 150


def test_no_account_keeps_legacy_whole_campaign():
    t = FakeTable(_rows(["111111111111", "222222222222"]))
    update_routed_via(t, "EKS:PLE", "default", None, "now")
    assert len(t.updated) == 6 and "FilterExpression" not in t.queries[0]


def test_invalid_account_falls_back_to_whole_campaign():
    t = FakeTable(_rows(["111111111111", "222222222222"]))
    update_routed_via(t, "EKS:PLE", "default", None, "now", account_id="not-an-acct")
    assert len(t.updated) == 6
