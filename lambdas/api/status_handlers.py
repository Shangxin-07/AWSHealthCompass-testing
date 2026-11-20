"""System status and cross-campaign account views.

GET /api/system/status  — background jobs, queues and alarms at a glance
GET /api/accounts       — per-account totals across campaigns (from stats)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

try:
    from lambdas.api import dashboard_handlers as dh  # pragma: no cover
except ImportError:  # Lambda runtime layout
    import dashboard_handlers as dh

from resolve_core import campaign_stats

logger = logging.getLogger(__name__)

# Fixed queue names (see the stacks); ServiceNow ones exist only when deployed.
QUEUES = (
    ("ingestion", "compass-ingestion", False),
    ("ingestionDlq", "compass-ingestion-dlq", True),
    ("jira", "compass-jira-integration", False),
    ("jiraDlq", "compass-jira-integration-dlq", True),
    ("servicenow", "compass-servicenow-integration", False),
    ("servicenowDlq", "compass-servicenow-dlq", True),
)
SYNC_STALE_SECONDS = 2 * 3600        # hourly schedule + slack
RECONCILE_STALE_SECONDS = 26 * 3600  # daily schedule + slack
_OPEN_STATUSES = ("ACTIVE", "PARTIAL")

_sqs = boto3.client("sqs")
_cloudwatch = boto3.client("cloudwatch")


def _age_seconds(ts: Any, now: datetime) -> Optional[int]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((now - dt).total_seconds())


def _num(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _sync_status(item: dict, now: datetime) -> dict:
    if not item:
        return {"state": "warning", "message": "Sync has never run.", "lastRunAt": None}
    last = item.get("updated_at")
    age = _age_seconds(last, now)
    result = item.get("last_sync_status", "unknown")
    out = {"lastRunAt": last, "ageSeconds": age, "result": result,
           "ticketsSynced": _num(item.get("tickets_synced")),
           "transitions": _num(item.get("status_transitions")),
           "errors": _num(item.get("errors"))}
    if result == "failed":
        out.update(state="error", message="Last Sync run failed.")
    elif age is None or age > SYNC_STALE_SECONDS:
        out.update(state="warning", message="Sync hasn't run in the last 2 hours.")
    elif out["errors"]:
        out.update(state="warning", message=f"Last Sync run had {out['errors']} error(s).")
    else:
        out.update(state="ok", message="Sync is running.")
    return out


def _reconcile_status(item: dict, now: datetime) -> dict:
    if not item:
        return {"state": "warning", "message": "Reconciliation has never run.", "lastRunAt": None}
    last = item.get("completed_at") or item.get("updated_at")
    age = _age_seconds(last, now)
    result = item.get("status", "unknown")
    codes = sorted({d.get("errorCode", "") for d in (item.get("error_details") or [])
                    if isinstance(d, dict) and d.get("errorCode")})
    out = {"lastRunAt": last, "ageSeconds": age, "result": result,
           "eventsFound": _num(item.get("events_found")),
           "eventsIngested": _num(item.get("events_ingested")),
           "errors": _num(item.get("errors")), "errorCodes": codes}
    if result == "failed":
        out.update(state="warning", message="Last Reconciliation failed" + (f" ({', '.join(codes)})." if codes else "."))
    elif result == "running":
        out.update(state="ok", message="Reconciliation is running.")
    elif age is None or age > RECONCILE_STALE_SECONDS:
        out.update(state="warning", message="Reconciliation hasn't completed in the last 26 hours.")
    elif result == "completed_with_errors":
        out.update(state="warning", message=f"Last Reconciliation had {out['errors']} error(s).")
    else:
        out.update(state="ok", message="Reconciliation is running daily.")
    return out


def _queue_depths() -> Dict[str, dict]:
    out = {}
    for key, name, is_dlq in QUEUES:
        try:
            url = _sqs.get_queue_url(QueueName=name)["QueueUrl"]
            attrs = _sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                              "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if "NonExistentQueue" in code or "QueueDoesNotExist" in code:
                continue  # integration not deployed
            logger.warning("Queue depth read failed — queue=%s code=%s", name, code)
            out[key] = {"name": name, "dlq": is_dlq, "error": True}
            continue
        out[key] = {"name": name, "dlq": is_dlq,
                    "visible": _num(attrs.get("ApproximateNumberOfMessages")),
                    "inFlight": _num(attrs.get("ApproximateNumberOfMessagesNotVisible"))}
    return out


def _alarms_firing() -> Optional[List[dict]]:
    try:
        firing = []
        kw = {"AlarmNamePrefix": "Compass", "StateValue": "ALARM"}
        while True:
            resp = _cloudwatch.describe_alarms(**kw)
            for a in resp.get("MetricAlarms", []):
                firing.append({"name": a.get("AlarmName", ""), "since": str(a.get("StateUpdatedTimestamp", ""))})
            if not resp.get("NextToken"):
                return firing
            kw["NextToken"] = resp["NextToken"]
    except ClientError:
        logger.warning("DescribeAlarms failed")
        return None


def handle_system_status(event, context):
    """GET /api/system/status."""
    now = datetime.now(timezone.utc)
    cfg = dh._config_table()
    try:
        sync = cfg.get_item(Key={"pk": "SYNC_STATE"}).get("Item") or {}
        recon = cfg.get_item(Key={"pk": "RECONCILE_STATE"}).get("Item") or {}
    except ClientError:
        logger.exception("Failed to read job state")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to read job state.")

    s, r = _sync_status(sync, now), _reconcile_status(recon, now)
    queues = _queue_depths()
    alarms = _alarms_firing()
    dlq_total = sum(q.get("visible", 0) + q.get("inFlight", 0) for q in queues.values() if q.get("dlq"))

    problems = []
    if s["state"] == "error":
        problems.append(("error", s["message"]))
    if dlq_total:
        problems.append(("error", f"{dlq_total} message(s) in dead-letter queues."))
    if alarms:
        problems.append(("error", f"{len(alarms)} alarm(s) firing."))
    if s["state"] == "warning":
        problems.append(("warning", s["message"]))
    if r["state"] == "warning":
        problems.append(("warning", r["message"]))
    overall = "error" if any(p[0] == "error" for p in problems) else "warning" if problems else "ok"

    return dh._success(200, {
        "overall": overall,
        "problems": [m for _, m in problems],
        "sync": s,
        "reconciliation": r,
        "queues": queues,
        "deadLetterMessages": dlq_total,
        "alarmsFiring": alarms,
        "checkedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def handle_accounts_summary(event, context):
    """GET /api/accounts?scope=active|all — per-account totals across campaigns."""
    params = event.get("queryStringParameters") or {}
    scope = params.get("scope", "active")
    if scope not in ("active", "all"):
        return dh._error(400, "INVALID_PARAM", "scope must be active or all.")
    try:
        table = dh._dynamodb.Table(dh.CAMPAIGNS_TABLE)
        campaigns, kw = [], {"ProjectionExpression": "campaignId, #s, startTime, statsVersion, campaignType",
                             "ExpressionAttributeNames": {"#s": "status"}}
        while True:
            resp = table.scan(**kw)
            campaigns.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except ClientError:
        logger.exception("Failed to scan campaigns")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to read campaigns.")
    if scope == "active":
        campaigns = [c for c in campaigns if c.get("status") in _OPEN_STATUSES]

    accounts: Dict[str, dict] = {}
    for c in campaigns:
        deadline = c.get("startTime") or None
        for item in dh._campaign_stats_items(c):
            a = campaign_stats.summarize_account(item)
            acc = accounts.setdefault(a["accountId"], {
                "accountId": a["accountId"], "total": 0, "pending": 0, "resolved": 0,
                "campaigns": 0, "campaignsWithPending": 0, "nearestDeadline": None})
            pending = a["total"] - a["resolved"]   # display semantics: not resolved
            acc["total"] += a["total"]
            acc["pending"] += pending
            acc["resolved"] += a["resolved"]
            acc["campaigns"] += 1
            if pending > 0:
                acc["campaignsWithPending"] += 1
                if deadline and (acc["nearestDeadline"] is None or deadline < acc["nearestDeadline"]):
                    acc["nearestDeadline"] = deadline

    names = dh._get_account_names([a for a in accounts if a != "unknown"])
    rows = []
    for acct, acc in accounts.items():
        acc["name"] = names.get(acct, "")
        rows.append(acc)
    rows.sort(key=lambda r: (-r["pending"], r["nearestDeadline"] or "9999", r["accountId"]))
    return dh._success(200, {"scope": scope, "campaigns": len(campaigns), "accounts": rows})
