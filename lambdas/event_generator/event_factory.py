"""Synthetic AWS Health EventBridge event factory — TEST ONLY.

Builds events in the shape the Processor Lambda parses from the ingestion
queue: an EventBridge envelope (source ``aws.health``) whose ``detail``
follows the AWS Health event schema, with the field variants Compass
handles (RFC 2822 dates, string ``page``/``totalPages``, ``resourceTags``
on entities, ``accountTags``, ``actionability``, ``backupEvent``).

This module was referenced by ``handler.py`` but absent from the
repository (it was git-ignored as a "synth-time artifact" with nothing in
the repo producing it), so the Event Generator failed at import.
"""
from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Dict, List, Optional

DEFAULT_REGION = "us-east-1"

_ARN_TEMPLATES = {
    "EKS": "arn:aws:eks:{region}:{account}:cluster/{rid}",
    "RDS": "arn:aws:rds:{region}:{account}:db:{rid}",
    "EC2": "arn:aws:ec2:{region}:{account}:instance/{rid}",
    "LAMBDA": "arn:aws:lambda:{region}:{account}:function:{rid}",
    "ELASTICACHE": "arn:aws:elasticache:{region}:{account}:cluster:{rid}",
    "S3": "arn:aws:s3:::{rid}",
}


def _rfc2822(dt: datetime) -> str:
    """EventBridge delivers Health dates in RFC 2822 form."""
    return format_datetime(dt, usegmt=True)


def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _resource_id(service: str, bare: bool) -> str:
    if service == "EC2" or bare:
        return "i-" + "".join(random.choices("0123456789abcdef", k=17))
    return f"compass-test-{service.lower()}-{_rand()}"


def make_resources(
    count: int = 3,
    service: str = "EKS",
    account: str = "111111111111",
    tags: Optional[Dict[str, str]] = None,
    bare_ids: bool = False,
    region: str = DEFAULT_REGION,
    status: str = "PENDING",
) -> List[Dict[str, Any]]:
    """Return ``count`` affected-entity dicts for one account.

    ``bare_ids=True`` emits bare instance IDs instead of ARNs, exercising
    the Processor's ARN generation path.
    """
    service = service.upper()
    template = _ARN_TEMPLATES.get(service, "arn:aws:{svc}:{region}:{account}:resource/{rid}")
    now = _rfc2822(datetime.now(timezone.utc))
    entities = []
    for _ in range(max(0, int(count))):
        rid = _resource_id(service, bare_ids)
        value = rid if bare_ids else template.format(
            region=region, account=account, rid=rid, svc=service.lower(),
        )
        entities.append({
            "entityValue": value,
            "awsAccountId": account,
            "lastUpdatedTime": now,
            "status": status,
            "resourceTags": dict(tags) if tags else {},
        })
    return entities


def make_health_event(
    service: str = "EKS",
    affected_account: str = "111111111111",
    event_type_code: Optional[str] = None,
    event_type_category: str = "scheduledChange",
    account_tags: Optional[Dict[str, str]] = None,
    resources: Optional[List[Dict[str, Any]]] = None,
    resource_count: int = 3,
    actionability: str = "ACTION_REQUIRED",
    backup_event: bool = False,
    page: str = "1",
    total_pages: str = "1",
    region: str = DEFAULT_REGION,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return one EventBridge-shaped AWS Health event.

    ``detail`` overrides individual detail fields (e.g. a shared
    ``eventArn`` for multi-page events).
    """
    service = service.upper()
    event_type_code = event_type_code or f"AWS_{service}_PLANNED_LIFECYCLE_EVENT"
    if resources is None:
        resources = make_resources(
            resource_count, service=service, account=affected_account, region=region,
        )

    now = datetime.now(timezone.utc)
    event_id = f"{event_type_code}_{uuid.uuid4().hex[:12]}"
    body: Dict[str, Any] = {
        "eventArn": f"arn:aws:health:{region}::event/{service}/{event_type_code}/{event_id}",
        "service": service,
        "eventScopeCode": "ACCOUNT_SPECIFIC",
        "communicationId": uuid.uuid4().hex,
        "eventTypeCode": event_type_code,
        "eventTypeCategory": event_type_category,
        "startTime": _rfc2822(now + timedelta(days=30)),
        "endTime": _rfc2822(now + timedelta(days=60)),
        "lastUpdatedTime": _rfc2822(now),
        "statusCode": "upcoming",
        "region": region,
        "eventRegion": region,
        "affectedAccount": affected_account,
        "actionability": actionability,
        "page": str(page),
        "totalPages": str(total_pages),
        "eventDescription": [{
            "language": "en_US",
            "latestDescription": (
                f"[Compass synthetic test event] {service} {event_type_code}. "
                "Not a real AWS Health notification."
            ),
        }],
        "affectedEntities": resources,
        "accountTags": dict(account_tags) if account_tags else {},
        "backupEvent": bool(backup_event),
    }
    if detail:
        body.update(detail)

    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "AWS Health Event",
        "source": "aws.health",
        "account": affected_account,
        "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": region,
        "resources": [e["entityValue"] for e in resources],
        "detail": body,
    }
