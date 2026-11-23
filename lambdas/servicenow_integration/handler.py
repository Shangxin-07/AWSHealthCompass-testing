"""ServiceNow Integration Lambda — entrypoint.

Receives standardized Health events from SQS (via SNS fan-out).
Instantiates ServiceNowClient + ServiceNowFormatter and delegates
to the ITSM orchestrator for ticket creation/update.

Same pattern as jira_integration/handler.py.
Trigger: SQS ServiceNow Queue (batch_size=1, ReportBatchItemFailures=true).
Runtime: Python 3.12, 256 MB, 5 min timeout, reserved concurrency 2.

Multi-platform routing — gate on routing.platforms.servicenow.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.servicenow_client import ServiceNowClient
from resolve_core.servicenow_formatter import ServiceNowFormatter
from resolve_core.itsm_client import ITSMAPIError
from resolve_core.config_schema import PK_SNOW_CONNECTION
from resolve_core.constants import TICKET_SUMMARY_PREFIX
from resolve_core.ticket_builder import format_account_display

logger = logging.getLogger("compass")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment ---
_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
_RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
_SNOW_SECRET_ARN = os.environ.get("SERVICENOW_SECRET_ARN", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Module-level resources (reused across warm invocations) ---
_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_config_table = _dynamodb.Table(_CONFIG_TABLE)

_client: ServiceNowClient | None = None
_enabled_platforms: Optional[list] = None


def _get_enabled_platforms() -> list:
    """Load and cache enabled platforms from ConfigTable.

    ServiceNow defaults to DISABLED (fail-closed) if INTEGRATIONS_ENABLED
    is missing, unlike JIRA which defaults to enabled.
    """
    global _enabled_platforms
    if _enabled_platforms is not None:
        return _enabled_platforms
    try:
        resp = _config_table.get_item(Key={"pk": "INTEGRATIONS_ENABLED"})
        item = resp.get("Item")
        if item and isinstance(item.get("platforms"), list):
            _enabled_platforms = item["platforms"]
        else:
            _enabled_platforms = ["jira"]  # backward compat — SNOW disabled by default
    except ClientError:
        _enabled_platforms = []  # fail-closed for ServiceNow
    return _enabled_platforms


def _get_client() -> ServiceNowClient:
    """Lazy-init ServiceNow client from ConfigTable."""
    global _client
    if _client:
        return _client

    resp = _config_table.get_item(Key={"pk": PK_SNOW_CONNECTION})
    conn = resp.get("Item")
    if not conn or not conn.get("validated"):
        raise RuntimeError("SNOW_CONNECTION not configured or not validated")

    instance_url = conn.get("instance_url", "")
    secret_arn = conn.get("secret_arn", _SNOW_SECRET_ARN)
    record_type = conn.get("record_type", "change_request")

    formatter = ServiceNowFormatter()
    _client = ServiceNowClient(
        instance_url=instance_url,
        secret_arn=secret_arn or _SNOW_SECRET_ARN,
        formatter=formatter,
        record_type=record_type,
    )
    return _client


def lambda_handler(event: dict, context: Any) -> dict:
    """SQS ESM handler. Processes Health events for ServiceNow ticketing.

    Returns {"batchItemFailures": []} on success, or
    {"batchItemFailures": [{"itemIdentifier": messageId}]} on failure.
    """
    # Global kill switch — fail-closed for ServiceNow
    if "servicenow" not in _get_enabled_platforms():
        logger.debug("ServiceNow platform globally disabled — skipping")
        return {"batchItemFailures": []}

    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            # SNS wraps the payload in a Message field
            payload = json.loads(body["Message"]) if "Message" in body else body

            # Per-event routing gate
            routing = payload.get("routing", {})
            platforms = routing.get("platforms", {})
            snow_target = platforms.get("servicenow")

            if not snow_target:
                logger.debug("No ServiceNow routing for event — skipping")
                continue  # Normal no-op — not an error

            event_arn = payload.get("event", {}).get("eventArn", "unknown")
            logger.info("snow_processing_start — event_arn=%s", event_arn)

            client = _get_client()
            _process_event(client, payload, snow_target)

            logger.info("snow_processing_complete — event_arn=%s", event_arn)

        except ITSMAPIError as exc:
            logger.error(
                "snow_api_error — message_id=%s status=%d retryable=%s msg=%s",
                message_id, exc.status_code, exc.retryable, exc.error_message[:200],
            )
            if exc.retryable:
                batch_item_failures.append({"itemIdentifier": message_id})
        except Exception:
            logger.exception("snow_processing_failed — message_id=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


def _create_ticket(client: ServiceNowClient, payload: dict, snow_target: dict) -> Optional[dict]:
    """Create a ServiceNow record for this event's account.

    Returns {ticketId, ticketRef, ticketUrl} or None when skipped.
    """
    from resolve_core.itsm_client import TicketCreateRequest
    from resolve_core.ticket_content import TicketContent

    event_data = payload.get("event", {})
    resources = payload.get("resources", [])

    campaign_id = event_data.get("campaignId", "")
    if not campaign_id:
        logger.warning("snow_no_campaign_id — skipping")
        return None

    # Extract routing target from platforms map
    assignment_group_id = snow_target["assignmentGroupId"]
    record_type = snow_target.get("recordType", "change_request")

    # Build ticket content
    account_display = format_account_display(event_data, payload.get("routing") or {})
    summary = f"{TICKET_SUMMARY_PREFIX} {event_data.get('service', '')} {event_data.get('eventTypeCode', '')} — {account_display}"
    if resources:
        summary += f" ({len(resources)} resources)"

    has_resources = bool(resources)
    campaign_type = "resource-level" if has_resources else "account-level"
    csv_needed = len(resources) > 100

    content = TicketContent(
        summary=summary[:160],
        metadata_pairs=[
            ("Service", event_data.get("service", "")),
            ("Deadline", event_data.get("startTime", "")),
            ("Account", account_display),
            ("Region", event_data.get("region", "")),
            ("Actionability", event_data.get("actionability", "")),
        ],
        description_text=event_data.get("description", ""),
        resources=resources[:100] if not csv_needed else [],
        guidance_text=event_data.get("startTime", ""),
        labels=[],
        due_date=event_data.get("startTime", "")[:10] if event_data.get("startTime") else None,
        campaign_type=campaign_type,
        csv_needed=csv_needed,
    )

    request = TicketCreateRequest(
        campaign_id=campaign_id,
        summary=summary[:160],
        description_content=content,
        routing_target=assignment_group_id,
        due_date=content.due_date,
        urgency=2,
        impact=2,
        labels=[],
        record_type=record_type,
        correlation_id=campaign_id,
    )

    ticket_resp = client.create_ticket(request)
    table = record_type if record_type in ("incident", "change_request") else "change_request"
    sys_id = (ticket_resp.raw_response or {}).get("sys_id", "")
    logger.info("snow_ticket_created — campaign=%s ticket=%s", campaign_id, ticket_resp.ticket_id)
    return {
        "ticketId": ticket_resp.ticket_id,            # number (e.g. CHG0030001); Sync matches on it
        "ticketRef": f"{table}/{sys_id}" if sys_id else "",  # what update/work-note calls need
        "ticketUrl": ticket_resp.ticket_url,
    }


def _event_action(payload: dict) -> str:
    """The Processor publishes event.action (metadata.action: older messages)."""
    event = payload.get("event") or {}
    meta = payload.get("metadata") or {}
    return str(event.get("action") or meta.get("action") or "CREATE")


def _account_rows(campaign_id: str, account_id: str) -> list:
    """This account's rows in the campaign (resource rows, or its ACCOUNT# row)."""
    from boto3.dynamodb.conditions import Attr, Key
    table = _dynamodb.Table(_RESOURCES_TABLE)
    kw = {"KeyConditionExpression": Key("campaignId").eq(campaign_id),
          "FilterExpression": Attr("accountId").eq(account_id)}
    rows = []
    while True:
        resp = table.query(**kw)
        rows.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return rows
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _existing_ticket(rows: list) -> Optional[dict]:
    """The account's ServiceNow ticket, if one was already created."""
    for r in rows:
        t = (r.get("tickets") or {}).get("servicenow")
        if isinstance(t, dict) and t.get("ticketId"):
            return {"ticketId": t["ticketId"], "ticketRef": t.get("ticketRef") or r.get("ticketRef", ""),
                    "ticketUrl": t.get("ticketUrl", "")}
        if r.get("ticketPlatform") == "servicenow" and r.get("ticketId"):
            return {"ticketId": r["ticketId"], "ticketRef": r.get("ticketRef", ""),
                    "ticketUrl": r.get("ticketUrl", "")}
    return None


def _record_ticket(campaign_id: str, rows: list, ticket: dict) -> int:
    """Write the ticket onto the account's existing rows. Never creates rows."""
    table = _dynamodb.Table(_RESOURCES_TABLE)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ticket_data = {"ticketId": ticket["ticketId"], "ticketRef": ticket["ticketRef"],
                   "ticketUrl": ticket["ticketUrl"], "ticketStatus": "Created",
                   "ticketRawStatus": "", "ticketUpdatedAt": now}
    values = {":td": ticket_data, ":tid": ticket["ticketId"], ":tref": ticket["ticketRef"],
              ":turl": ticket["ticketUrl"], ":tp": "servicenow", ":ts": "Created", ":tua": now}
    flat = ("ticketId = :tid, ticketRef = :tref, ticketUrl = :turl, ticketPlatform = :tp, "
            "ticketStatus = :ts, ticketUpdatedAt = :tua")
    written = 0
    for r in rows:
        key = {"campaignId": campaign_id, "trackingKey": r["trackingKey"]}
        try:
            try:
                table.update_item(Key=key, UpdateExpression=f"SET #t.#p = :td, {flat}",
                                  ExpressionAttributeNames={"#t": "tickets", "#p": "servicenow"},
                                  ExpressionAttributeValues=values,
                                  ConditionExpression="attribute_exists(campaignId)")
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ValidationException":
                    raise
                # tickets map missing (legacy row): set the whole map
                vals = dict(values)
                vals[":tm"] = {"servicenow": vals.pop(":td")}
                table.update_item(Key=key, UpdateExpression=f"SET #t = :tm, {flat}",
                                  ExpressionAttributeNames={"#t": "tickets"},
                                  ExpressionAttributeValues=vals,
                                  ConditionExpression="attribute_exists(campaignId)")
            written += 1
        except ClientError as exc:
            logger.warning("snow_ticket_record_failed — campaign=%s code=%s", campaign_id,
                           exc.response.get("Error", {}).get("Code", ""))
    return written


def _update_ticket(client: ServiceNowClient, payload: dict, ticket: dict, rows: list) -> None:
    """Reflect this account's resource progress on its ServiceNow record."""
    event_data = payload.get("event", {})
    if not ticket.get("ticketRef"):
        logger.warning("snow_update_skipped — no ticketRef (ticket created before refs were stored) — ticket=%s",
                       ticket.get("ticketId"))
        return
    pending = sum(1 for r in rows if r.get("healthStatus") != "RESOLVED")
    resolved = len(rows) - pending
    account_display = format_account_display(event_data, payload.get("routing") or {})
    summary = (f"{TICKET_SUMMARY_PREFIX} {event_data.get('service', '')} {event_data.get('eventTypeCode', '')} — "
               f"{account_display} ({pending} pending, {resolved} resolved)")
    client.update_ticket(ticket["ticketRef"], {"short_description": summary[:160]})

    newly = {str(a) for a in (payload.get("newlyResolved") or [])}
    mine = [r.get("entityValue") or r["trackingKey"] for r in rows
            if (r.get("entityValue") or r["trackingKey"]) in newly]
    if mine:
        shown = mine[:_MAX_NOTE_RESOURCES]
        more = len(mine) - len(shown)
        note = (f"AWS Health reports {len(mine)} resource(s) resolved:\n"
                + "\n".join(f"- {arn}" for arn in shown)
                + (f"\n…and {more} more" if more else "")
                + f"\n\nRemaining pending: {pending} of {len(rows)}.")
        client.add_work_note(ticket["ticketRef"], note)
    logger.info("snow_ticket_updated — ticket=%s pending=%d resolved=%d newly_resolved=%d",
                ticket["ticketId"], pending, resolved, len(mine))


_MAX_NOTE_RESOURCES = 50


def _process_event(client: ServiceNowClient, payload: dict, snow_target: dict) -> None:
    """Create or update the ServiceNow record for this event's account.

    One record per account per campaign. CREATE/RECONCILE creates it if the
    account has none; RESOURCE_UPDATE updates it, or creates it when this is
    the account's first event in an existing (merged) campaign.
    """
    event_data = payload.get("event", {})
    campaign_id = event_data.get("campaignId", "")
    account_id = event_data.get("affectedAccount", "")
    if not campaign_id:
        logger.warning("snow_no_campaign_id — skipping")
        return
    action = _event_action(payload)
    rows = _account_rows(campaign_id, account_id) if account_id else []
    existing = _existing_ticket(rows)

    if existing:
        if action == "RESOURCE_UPDATE":
            _update_ticket(client, payload, existing, rows)
        else:
            logger.info("snow_duplicate_skipped — campaign=%s ticket=%s", campaign_id, existing["ticketId"])
        return

    ticket = _create_ticket(client, payload, snow_target)
    if not ticket:
        return
    if not rows:
        logger.warning("snow_ticket_unrecorded — no rows for account %s in campaign %s; ticket=%s",
                       account_id, campaign_id, ticket["ticketId"])
        return
    _record_ticket(campaign_id, rows, ticket)
