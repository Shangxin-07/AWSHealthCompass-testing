"""Per-campaign, per-account resource counters (compass-campaign-stats).

Replaces re-reading every resource row of a campaign on every event (O(n²)
reads on one partition for org-wide events) and on every dashboard request.

Table: pk ``campaignId``, sk ``accountId``. One small item per account:

    total, pending, resolved        health counts (same semantics as the
                                    legacy recount: account-level rows have
                                    no healthStatus, so count in total only)
    t_<ticket>, t_<ticket>_res      rows per ticket status, and how many of
                                    those are RESOLVED (cross-tab)

Write paths:
  * Processor / Reconciliation: :func:`apply_deltas` — atomic ``ADD`` per
    touched account, then :func:`refresh_campaign_totals` sums the stats
    partition (a few hundred small items) and SETs absolute campaign totals.
  * Hourly Sync: :func:`recount_campaign` — authoritative full recount that
    overwrites the stats items. It heals drift from retries or partial
    failures and is the source of truth for ticket-status counts (ticket
    statuses change in the ITSM, not in Compass).

Campaigns created by this version carry ``statsVersion``. A campaign without
it (created before this table existed) is recounted on first touch.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

STATS_VERSION = 1

# Ticket statuses Compass writes (resources.py / ITSM handlers / Sync).
_TICKET_ATTR = {
    "none": "t_none",
    "Created": "t_created",
    "In Progress": "t_in_progress",
    "Closed": "t_closed",
    "unknown": "t_unknown",
}
TICKET_STATUSES = tuple(_TICKET_ATTR)
_HEALTH_FIELDS = ("total", "pending", "resolved")
_ACCOUNT_RE = re.compile(r"^\d{12}$")


def _ticket_attr(status: Any) -> str:
    return _TICKET_ATTR.get(status if isinstance(status, str) else "", "t_unknown")


def _is_resolved(health: Any) -> bool:
    return isinstance(health, str) and health.upper() == "RESOLVED"


def _is_pending(health: Any) -> bool:
    # Legacy semantics: any non-None, non-RESOLVED status is pending;
    # account-level rows (healthStatus None) count in total only.
    return health is not None and not _is_resolved(health)


def _account_key(account_id: Any) -> str:
    return account_id if isinstance(account_id, str) and _ACCOUNT_RE.match(account_id) else "unknown"


class Deltas:
    """Accumulates counter changes per account for one write batch."""

    def __init__(self) -> None:
        self._d: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def new_row(self, account_id: Any, health: Any, ticket_status: Any = "none") -> None:
        d = self._d[_account_key(account_id)]
        t = _ticket_attr(ticket_status)
        d["total"] += 1
        d[t] += 1
        if _is_resolved(health):
            d["resolved"] += 1
            d[t + "_res"] += 1
        elif _is_pending(health):
            d["pending"] += 1

    def changed_row(self, account_id: Any, old_health: Any, new_health: Any,
                    ticket_status: Any) -> None:
        if old_health == new_health:
            return
        d = self._d[_account_key(account_id)]
        t = _ticket_attr(ticket_status)
        d["resolved"] += int(_is_resolved(new_health)) - int(_is_resolved(old_health))
        d["pending"] += int(_is_pending(new_health)) - int(_is_pending(old_health))
        d[t + "_res"] += int(_is_resolved(new_health)) - int(_is_resolved(old_health))

    def items(self):
        for acct, d in self._d.items():
            nz = {k: v for k, v in d.items() if v}
            if nz:
                yield acct, nz

    def __bool__(self) -> bool:
        return any(True for _ in self.items())


def _query_all(table: Any, campaign_id: str, projection: Optional[str] = None) -> List[dict]:
    kw: Dict[str, Any] = {
        "KeyConditionExpression": "campaignId = :c",
        "ExpressionAttributeValues": {":c": campaign_id},
    }
    if projection:
        kw["ProjectionExpression"] = projection
    out: List[dict] = []
    while True:
        r = table.query(**kw)
        out.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def read_stats(stats_table: Any, campaign_id: str) -> List[dict]:
    """All per-account stats items for a campaign."""
    return _query_all(stats_table, campaign_id)


def _sum(stats_items: Iterable[dict]) -> Dict[str, int]:
    tot = {k: 0 for k in _HEALTH_FIELDS}
    for it in stats_items:
        for k in _HEALTH_FIELDS:
            tot[k] += int(it.get(k, 0) or 0)
    return tot


def refresh_campaign_totals(stats_table: Any, campaigns_table: Any,
                            campaign_id: str, now: str) -> Dict[str, int]:
    """SET absolute campaign totals from the (small) stats partition."""
    tot = _sum(read_stats(stats_table, campaign_id))
    _set_campaign_totals(campaigns_table, campaign_id, tot, now)
    return tot


def _set_campaign_totals(campaigns_table: Any, campaign_id: str,
                         tot: Dict[str, int], now: str, extra: Optional[dict] = None) -> None:
    names = {"#trc": "totalResourceCount", "#pc": "pendingCount",
             "#rc": "resolvedCount", "#ua": "updatedAt", "#sv": "statsVersion"}
    values = {":trc": tot["total"], ":pc": tot["pending"], ":rc": tot["resolved"],
              ":ua": now, ":sv": STATS_VERSION}
    sets = ["#trc = :trc", "#pc = :pc", "#rc = :rc", "#ua = :ua", "#sv = :sv"]
    for i, (k, v) in enumerate((extra or {}).items()):
        names[f"#x{i}"] = k
        values[f":x{i}"] = v
        sets.append(f"#x{i} = :x{i}")
    try:
        campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(campaignId)",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.error("Campaign totals update failed — campaign_id=%s code=%s",
                     campaign_id[:256], code)


def apply_deltas(stats_table: Any, campaigns_table: Any, campaign_id: str,
                 deltas: Deltas, now: str) -> Dict[str, int]:
    """ADD per-account deltas, then refresh campaign totals. Returns totals."""
    for acct, d in deltas.items():
        names = {f"#a{i}": k for i, k in enumerate(d)}
        values = {f":a{i}": v for i, v in enumerate(d.values())}
        names["#ua"] = "updatedAt"
        values[":ua"] = now
        adds = ", ".join(f"#a{i} :a{i}" for i in range(len(d)))
        stats_table.update_item(
            Key={"campaignId": campaign_id, "accountId": acct},
            UpdateExpression=f"ADD {adds} SET #ua = :ua",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    return refresh_campaign_totals(stats_table, campaigns_table, campaign_id, now)


def recount_campaign(resources_table: Any, stats_table: Any, campaigns_table: Any,
                     campaign_id: str, now: str) -> Dict[str, int]:
    """Authoritative recount: rebuild every stats item from the resource rows."""
    rows = _query_all(resources_table, campaign_id,
                      "accountId, healthStatus, ticketStatus")
    agg = Deltas()
    for r in rows:
        agg.new_row(r.get("accountId"), r.get("healthStatus"), r.get("ticketStatus") or "none")
    built = {acct: d for acct, d in agg.items()}

    existing = {it["accountId"] for it in read_stats(stats_table, campaign_id)}
    with stats_table.batch_writer() as b:
        for acct, d in built.items():
            item = {"campaignId": campaign_id, "accountId": acct,
                    "updatedAt": now, "recountedAt": now}
            item.update({k: 0 for k in _HEALTH_FIELDS})
            item.update(d)
            b.put_item(Item=item)
        for acct in existing - set(built):
            b.delete_item(Key={"campaignId": campaign_id, "accountId": acct})

    tot = {k: sum(d.get(k, 0) for d in built.values()) for k in _HEALTH_FIELDS}
    _set_campaign_totals(campaigns_table, campaign_id, tot, now,
                         extra={"statsRecountedAt": now})
    return tot


def ensure_stats(resources_table: Any, stats_table: Any, campaigns_table: Any,
                 campaign: dict, now: str) -> List[dict]:
    """Stats items for a campaign, recounting first if it has never been tracked."""
    cid = campaign["campaignId"]
    if not campaign.get("statsVersion"):
        recount_campaign(resources_table, stats_table, campaigns_table, cid, now)
    return read_stats(stats_table, cid)


def summarize_account(item: dict) -> Dict[str, Any]:
    """Shape one stats item for API responses."""
    tickets = {}
    for status, attr in _TICKET_ATTR.items():
        n = int(item.get(attr, 0) or 0)
        if n:
            tickets[status] = n
    t_none = int(item.get("t_none", 0) or 0)
    t_none_res = int(item.get("t_none_res", 0) or 0)
    return {
        "accountId": item.get("accountId", "unknown"),
        "total": int(item.get("total", 0) or 0),
        "pending": int(item.get("pending", 0) or 0),
        "resolved": int(item.get("resolved", 0) or 0),
        "ticketStatus": tickets,
        "unticketedUnresolved": max(0, t_none - t_none_res),
    }


def ticket_status_groups(stats_items: Iterable[dict]) -> Dict[str, Dict[str, int]]:
    """Cross-account totals per ticket status: {status: {total, resolved, pending}}."""
    out: Dict[str, Dict[str, int]] = {}
    for it in stats_items:
        for status, attr in _TICKET_ATTR.items():
            n = int(it.get(attr, 0) or 0)
            if not n:
                continue
            res = int(it.get(attr + "_res", 0) or 0)
            g = out.setdefault(status, {"total": 0, "resolved": 0, "pending": 0})
            g["total"] += n
            g["resolved"] += res
            g["pending"] += n - res
    return out
