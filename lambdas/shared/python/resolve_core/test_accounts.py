"""Named synthetic accounts for the test tools (Route Test, Bulk Generation).

Fake 12-digit IDs (600000000001-...) with readable names, so test campaigns,
tickets and breakdowns are easy to follow. ``ensure_registered`` creates an
account mapping (ROUTING#<id>) for each one that doesn't have one yet, with
the name and the default routing target; existing mappings are never touched.

The Event Generator (no shared layer) keeps a copy of the IDs as its default.
"""
from __future__ import annotations

from typing import Any, Dict, List

from botocore.exceptions import ClientError

TEST_ACCOUNTS: List[tuple] = [
    ("600000000001", "lt-finance-prod"),
    ("600000000002", "lt-finance-dev"),
    ("600000000003", "lt-research-hpc"),
    ("600000000004", "lt-student-portal-prod"),
    ("600000000005", "lt-student-portal-test"),
    ("600000000006", "lt-library-systems"),
    ("600000000007", "lt-identity-prod"),
    ("600000000008", "lt-data-platform"),
    ("600000000009", "lt-network-shared"),
    ("600000000010", "lt-security-tooling"),
]
TEST_ACCOUNT_IDS = [a for a, _ in TEST_ACCOUNTS]
_NAMES = dict(TEST_ACCOUNTS)


def is_test_account(account_id: Any) -> bool:
    return account_id in _NAMES


def list_accounts(config_table: Any) -> List[Dict[str, Any]]:
    """Catalogue with each account's current mapping (if any)."""
    out = []
    for acct, name in TEST_ACCOUNTS:
        item = config_table.get_item(Key={"pk": f"ROUTING#{acct}"}).get("Item") or {}
        out.append({
            "accountId": acct,
            "name": item.get("account_name") or name,
            "registered": bool(item),
            "jiraProject": item.get("jira_project", ""),
        })
    return out


def ensure_registered(config_table: Any, account_ids: List[str], now: str) -> int:
    """Create name mappings for catalogue accounts that have none. Returns count created."""
    default = config_table.get_item(Key={"pk": "ROUTING_DEFAULT"}).get("Item") or {}
    created = 0
    for acct in account_ids:
        if acct not in _NAMES:
            continue
        item = {"pk": f"ROUTING#{acct}", "account_id": acct, "account_name": _NAMES[acct],
                "jira_issue_type": default.get("jira_issue_type", "Task"), "updated_at": now,
                "createdBy": "test-tools"}
        if default.get("jira_project"):
            item["jira_project"] = default["jira_project"]
        if default.get("snow_assignment_group_id"):
            item["snow_assignment_group_id"] = default["snow_assignment_group_id"]
        try:
            config_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            created += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return created
