"""Alert Approvals: whitelist which AWS Health alert types reach the ITSM.

GET  /api/alerts                 alert types seen in campaigns + approval state
POST /api/alerts/whitelist       {"enabled": bool} — whitelist mode on/off
POST /api/alerts/approve         {"eventTypeCode", "eventTypeCategory"}
POST /api/alerts/revoke          {"eventTypeCode", "eventTypeCategory"}
POST /api/alerts/send            {"eventTypeCode", "eventTypeCategory", "dryRun"}

Whitelist mode is the dispatch window's "custom" mode: only event types
matching an enabled DISPATCH_RULE# go to the ITSM; others are recorded as
held-back (FILTERED) campaigns. Approvals are exact-type rules in the same
format the Dispatch Window settings use, plus who approved them and when.
Approving affects new events only; "send" releases already-recorded
campaigns of an approved type through the existing Create Tickets path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

try:
    from lambdas.api import dashboard_handlers as dh  # pragma: no cover
except ImportError:  # Lambda runtime layout
    import dashboard_handlers as dh

from resolve_core import campaign_stats
from resolve_core.dispatch import evaluate_dispatch

logger = logging.getLogger(__name__)

# Exact alert types; hyphens occur in real ones (e.g. AWS_DEVOPS-GURU_...).
_EVENT_TYPE_RE = re.compile(r"^AWS_[A-Z0-9_-]{1,200}$")
_CATEGORIES = ("scheduledChange", "accountNotification")
_RULE_PREFIX = "approved-"
_MAX_RULES = 100            # resolve_core.dispatch truncates beyond this
_MAX_SEND_CAMPAIGNS = 50    # per request (API Gateway 29s limit)


def _actor(event: dict) -> str:
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    return str(auth.get("userId") or auth.get("principalId") or "unknown")[:256]


def _rule_id(code: str, category: str) -> str:
    return _RULE_PREFIX + hashlib.sha1(f"{code}|{category}".encode()).hexdigest()[:16]


def _scan(table, **kw) -> List[dict]:
    out: List[dict] = []
    while True:
        r = table.scan(**kw)
        out.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _dispatch_state() -> tuple:
    """(preset item, list of rule items)."""
    cfg = dh._config_table()
    preset = cfg.get_item(Key={"pk": "DISPATCH_PRESET"}).get("Item") or {}
    rules = _scan(cfg, FilterExpression="begins_with(pk, :p)",
                  ExpressionAttributeValues={":p": "DISPATCH_RULE#"})
    rules.sort(key=lambda r: r.get("rule_id", ""))
    return preset, rules


def _parse_type(event: dict) -> tuple:
    body = dh._parse_body(event)
    if not isinstance(body, dict):
        return None, None, dh._error(400, "INVALID_REQUEST", "Request body must be a JSON object.")
    code, cat = body.get("eventTypeCode"), body.get("eventTypeCategory")
    if not (isinstance(code, str) and _EVENT_TYPE_RE.match(code)):
        return None, None, dh._error(400, "INVALID_PARAM", "eventTypeCode must be an AWS Health event type code.")
    if cat not in _CATEGORIES:
        return None, None, dh._error(400, "INVALID_PARAM", "eventTypeCategory must be scheduledChange or accountNotification.")
    return code, cat, None


def _campaigns_of(code: Optional[str] = None, cat: Optional[str] = None) -> List[dict]:
    items = _scan(dh._dynamodb.Table(dh.CAMPAIGNS_TABLE), ProjectionExpression=(
        "campaignId, service, eventTypeCode, eventTypeCategory, #s, dispatched, statsVersion, "
        "totalResourceCount, updatedAt, createdAt, campaignType"),
        ExpressionAttributeNames={"#s": "status"})
    if code is not None:
        items = [c for c in items if c.get("eventTypeCode") == code and c.get("eventTypeCategory") == cat]
    return items


def _ticketable_accounts(campaign: dict) -> int:
    """Accounts in the campaign with unticketed, unresolved resources."""
    return sum(1 for it in dh._campaign_stats_items(campaign)
               if campaign_stats.summarize_account(it)["unticketedUnresolved"] > 0)


# ---------------------------------------------------------------- GET /api/alerts

def handle_alerts_list(event, context):
    try:
        preset, rules = _dispatch_state()
        campaigns = _campaigns_of()
    except ClientError:
        logger.exception("Failed to read alert data")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to read alert data.")
    mode = preset.get("mode", "all")
    config = {"mode": mode, "actionability_filter": preset.get("actionability_filter", "all_actionable"),
              "rules": rules}
    by_id = {r.get("rule_id"): r for r in rules}

    types: Dict[tuple, dict] = {}
    for c in campaigns:
        key = (c.get("eventTypeCode", ""), c.get("eventTypeCategory", ""))
        t = types.setdefault(key, {
            "eventTypeCode": key[0], "eventTypeCategory": key[1], "service": c.get("service", ""),
            "campaigns": 0, "heldBack": 0, "accounts": set(), "resources": 0,
            "ticketableAccounts": 0, "lastSeen": ""})
        t["campaigns"] += 1
        if c.get("dispatched") is False or c.get("status") == "FILTERED":
            t["heldBack"] += 1
        t["resources"] += int(c.get("totalResourceCount", 0) or 0)
        t["lastSeen"] = max(t["lastSeen"], c.get("updatedAt") or c.get("createdAt") or "")
        for it in dh._campaign_stats_items(c):
            a = campaign_stats.summarize_account(it)
            t["accounts"].add(a["accountId"])
            if a["unticketedUnresolved"] > 0:
                t["ticketableAccounts"] += 1

    out = []
    for (code, cat), t in types.items():
        decision = evaluate_dispatch(code, cat, config)
        rule = by_id.get(_rule_id(code, cat))
        approval = None
        if rule and rule.get("enabled"):
            approval = {"ruleId": rule["rule_id"], "approvedBy": rule.get("approved_by", ""),
                        "approvedAt": rule.get("approved_at", "")}
        t["accounts"] = len(t["accounts"])
        t.update({
            "approved": bool(decision.get("dispatched")) if mode == "custom" else None,
            "approval": approval,
            # Matched by a rule other than this tab's own (e.g. a prefix rule
            # from the Dispatch Window settings).
            "approvedByOtherRule": (decision.get("matchedRule") not in (None, _rule_id(code, cat))
                                    if mode == "custom" and decision.get("dispatched") else False),
            "revoked": ({"revokedBy": rule.get("revoked_by", ""), "revokedAt": rule.get("revoked_at", "")}
                        if rule and not rule.get("enabled") else None),
        })
        out.append(t)
    out.sort(key=lambda t: (t["approved"] is True, -t["ticketableAccounts"], t["eventTypeCode"]))
    return dh._success(200, {
        "whitelistEnabled": mode == "custom",
        "mode": mode,
        "rulesUsed": len([r for r in rules if r.get("enabled")]),
        "ruleLimit": _MAX_RULES,
        "types": out,
    })


# ---------------------------------------------------------------- whitelist mode

def handle_alerts_whitelist(event, context):
    body = dh._parse_body(event)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return dh._error(400, "INVALID_PARAM", "enabled (boolean) is required.")
    try:
        cfg = dh._config_table()
        preset = cfg.get_item(Key={"pk": "DISPATCH_PRESET"}).get("Item") or {}
        cfg.put_item(Item={
            "pk": "DISPATCH_PRESET",
            # On: only approved types are sent. Off: every actionable type.
            "mode": "custom" if body["enabled"] else "all",
            "actionability_filter": preset.get("actionability_filter", "all_actionable"),
            "updated_at": dh._now_iso(),
            "updated_by": _actor(event),
        })
    except ClientError:
        logger.exception("Failed to update whitelist mode")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to update whitelist mode.")
    logger.info("Alert whitelist %s — by=%s", "enabled" if body["enabled"] else "disabled", _actor(event))
    return dh._success(200, {"whitelistEnabled": body["enabled"]})


# ---------------------------------------------------------------- approve / revoke

def handle_alerts_approve(event, context):
    code, cat, err = _parse_type(event)
    if err:
        return err
    try:
        _, rules = _dispatch_state()
        rid = _rule_id(code, cat)
        active = [r for r in rules if r.get("enabled") and r.get("rule_id") != rid]
        if len(active) >= _MAX_RULES:
            return dh._error(400, "RULE_LIMIT", f"At most {_MAX_RULES} enabled dispatch rules are supported.")
        now = dh._now_iso()
        dh._config_table().put_item(Item={
            "pk": f"DISPATCH_RULE#{rid}", "rule_id": rid,
            "event_type_pattern": code, "event_categories": [cat], "enabled": True,
            "source": "alert-approvals", "approved_by": _actor(event), "approved_at": now,
            "created_at": now, "updated_at": now,
        })
    except ClientError:
        logger.exception("Failed to approve alert type")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to approve alert type.")
    logger.info("Alert type approved — type=%s category=%s by=%s", code, cat, _actor(event))
    return dh._success(200, {"eventTypeCode": code, "eventTypeCategory": cat, "approved": True, "ruleId": rid})


def handle_alerts_revoke(event, context):
    code, cat, err = _parse_type(event)
    if err:
        return err
    rid = _rule_id(code, cat)
    try:
        dh._config_table().update_item(
            Key={"pk": f"DISPATCH_RULE#{rid}"},
            UpdateExpression="SET enabled = :f, revoked_by = :b, revoked_at = :t, updated_at = :t",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":f": False, ":b": _actor(event), ":t": dh._now_iso()},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return dh._error(404, "NOT_FOUND", "This alert type has no approval to revoke.")
        logger.exception("Failed to revoke alert type")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to revoke alert type.")
    logger.info("Alert type revoked — type=%s category=%s by=%s", code, cat, _actor(event))
    return dh._success(200, {"eventTypeCode": code, "eventTypeCategory": cat, "approved": False})


# ---------------------------------------------------------------- send held-back campaigns

def handle_alerts_send(event, context):
    code, cat, err = _parse_type(event)
    if err:
        return err
    body = dh._parse_body(event) or {}
    dry_run = body.get("dryRun", True) is not False   # default: preview only
    try:
        preset, rules = _dispatch_state()
        config = {"mode": preset.get("mode", "all"), "rules": rules,
                  "actionability_filter": preset.get("actionability_filter", "all_actionable")}
        if config["mode"] == "custom" and not evaluate_dispatch(code, cat, config).get("dispatched"):
            return dh._error(400, "NOT_APPROVED", "Approve this alert type before sending its campaigns.")
        candidates = []
        for c in _campaigns_of(code, cat):
            n = _ticketable_accounts(c)
            if n:
                candidates.append((c, n))
    except ClientError:
        logger.exception("Failed to read campaigns for send")
        return dh._error(500, "SYS_INTERNAL_ERROR", "Failed to read campaigns.")

    summary = {"eventTypeCode": code, "eventTypeCategory": cat, "dryRun": dry_run,
               "campaigns": len(candidates), "tickets": sum(n for _, n in candidates),
               "limit": _MAX_SEND_CAMPAIGNS}
    if dry_run:
        return dh._success(200, summary)
    if len(candidates) > _MAX_SEND_CAMPAIGNS:
        return dh._error(400, "TOO_MANY", f"{len(candidates)} campaigns; at most {_MAX_SEND_CAMPAIGNS} per request.")

    results = []
    for c, n in candidates:
        cid = c["campaignId"]
        resp = dh.handle_create_tickets({"pathParameters": {"id": cid}, "body": "{}",
                                         "requestContext": event.get("requestContext") or {}}, context)
        ok = resp.get("statusCode", 500) < 300
        results.append({"campaignId": cid, "ok": ok, "tickets": n,
                        "error": None if ok else (json.loads(resp.get("body") or "{}").get("error") or {}).get("message")})
        if ok:
            try:  # no longer held back
                expr = "SET dispatched = :t, dispatchMode = :m, updatedAt = :u"
                vals = {":t": True, ":m": "alert-approvals", ":u": dh._now_iso()}
                names = None
                if c.get("status") == "FILTERED":
                    expr += ", #s = :a"
                    vals[":a"] = "ACTIVE"
                    names = {"#s": "status"}
                kw = {"Key": {"campaignId": cid}, "UpdateExpression": expr, "ExpressionAttributeValues": vals}
                if names:
                    kw["ExpressionAttributeNames"] = names
                dh._dynamodb.Table(dh.CAMPAIGNS_TABLE).update_item(**kw)
            except ClientError:
                logger.warning("Failed to mark campaign dispatched — campaign_id=%s", cid[:256])
    summary.update({"sent": sum(1 for r in results if r["ok"]), "failed": [r for r in results if not r["ok"]]})
    logger.info("Held-back campaigns sent — type=%s campaigns=%d by=%s", code, summary["sent"], _actor(event))
    return dh._success(200, summary)
