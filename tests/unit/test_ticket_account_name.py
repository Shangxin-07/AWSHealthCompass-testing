"""Tickets show the routing record's account name next to the account ID."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))

from resolve_core import ticket_builder as tb  # noqa: E402
from resolve_core.routing import resolve_account_routing, resolve_routing  # noqa: E402

ACCT = "600000000001"
CACHE = {
    f"ROUTING#{ACCT}": {"pk": f"ROUTING#{ACCT}", "jira_project": "KAN",
                        "jira_issue_type": "Task", "account_name": "lt-finance-prod"},
    "ROUTING_DEFAULT": {"pk": "ROUTING_DEFAULT", "jira_project": "DEF", "jira_issue_type": "Task"},
}
EVENT = {"service": "EKS", "eventTypeCode": "AWS_EKS_PLANNED_LIFECYCLE_EVENT",
         "affectedAccount": ACCT, "accountTags": {}, "region": "us-east-1"}


def test_routing_result_carries_account_name():
    assert resolve_account_routing(ACCT, CACHE)["accountName"] == "lt-finance-prod"
    r = resolve_routing({"affectedAccount": ACCT, "service": "EKS"}, {}, {}, [], CACHE)
    assert r["accountName"] == "lt-finance-prod"


def test_default_routed_account_still_named_if_record_has_name_only():
    cache = dict(CACHE)
    cache[f"ROUTING#{ACCT}"] = {"pk": f"ROUTING#{ACCT}", "account_name": "named-no-target"}
    r = resolve_account_routing(ACCT, cache)
    assert r["resolvedBy"] == "default" and r["accountName"] == "named-no-target"


def test_unknown_account_has_empty_name():
    assert resolve_account_routing("999999999999", CACHE)["accountName"] == ""


def test_display_prefers_routing_name_then_tag_then_id():
    assert tb._format_account_display(EVENT, {"accountName": "lt-finance-prod"}) == f"{ACCT} (lt-finance-prod)"
    tagged = {**EVENT, "accountTags": {"Name": "from-tag"}}
    assert tb._format_account_display(tagged, {}) == f"{ACCT} (from-tag)"
    assert tb._format_account_display(EVENT, None) == ACCT


def test_resource_ticket_summary_and_metadata_include_name():
    routing = resolve_account_routing(ACCT, CACHE)
    summary = tb._build_summary(EVENT, routing, 3)
    assert f"{ACCT} (lt-finance-prod) (3 resources)" in summary
    meta = str(tb._build_metadata(EVENT, routing))
    assert "lt-finance-prod" in meta
