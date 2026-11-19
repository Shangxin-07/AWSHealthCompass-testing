"""Unit tests for reconciliation time-budget continuation (no AWS access)."""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
ARN = "arn:aws:health:us-east-1::event/EC2/AWS_EC2_X/{}"


@pytest.fixture()
def rec():
    sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
    spec = importlib.util.spec_from_file_location(
        "recon", os.path.join(ROOT, "lambdas/reconciliation/handler.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._config_table = MagicMock()
    m._lambda_client = MagicMock()
    m._load_config = lambda: {}
    return m


class Ctx:
    """Fake Lambda context: reports 'low time' after n calls."""
    def __init__(self, ok_calls):
        self.n = ok_calls
    def get_remaining_time_in_millis(self):
        self.n -= 1
        return 900_000 if self.n >= 0 else 1_000


def _events(rec, n):
    evs = [{"arn": ARN.format(i)} for i in range(n)]
    rec._list_health_events = lambda *a, **k: sorted(evs, key=lambda e: e["arn"])
    return evs


def _payload(**kw):
    base = {"source": "reconciliation-continuation", "runId": "deadbeef-1234",
            "startedAt": "2026-01-01T00:00:00Z", "lookbackStart": "2025-12-30T00:00:00Z",
            "cursor": "", "depth": 0, "counters": {}}
    base.update(kw)
    return base


def test_completes_in_one_run_with_plenty_of_time(rec):
    _events(rec, 5)
    rec._process_single_event = lambda e, c, n: "ingested"
    out = rec.lambda_handler({"detail-type": "Scheduled Event"}, Ctx(100))
    assert out["status"] == "completed" and out["events_ingested"] == 5
    rec._lambda_client.invoke.assert_not_called()


def test_hands_off_with_cursor_then_resumes(rec):
    _events(rec, 6)
    processed = []
    rec._process_single_event = lambda e, c, n: processed.append(e["arn"]) or "ingested"

    out = rec.lambda_handler({"detail-type": "Scheduled Event"}, Ctx(3))
    assert out["status"] == "continued"
    assert processed == [ARN.format(i) for i in range(3)]
    payload = json.loads(rec._lambda_client.invoke.call_args.kwargs["Payload"])
    assert payload["source"] == "reconciliation-continuation"
    assert payload["cursor"] == ARN.format(2)
    assert payload["counters"]["events_ingested"] == 3
    assert payload["counters"]["events_found"] == 6

    out = rec.lambda_handler(payload, Ctx(100))
    assert out["status"] == "completed"
    assert processed == [ARN.format(i) for i in range(6)]  # no repeats
    assert out["events_ingested"] == 6 and out["events_found"] == 6


def test_continuation_requires_run_ownership(rec):
    _events(rec, 2)
    rec._config_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    out = rec.lambda_handler(_payload(), Ctx(100))
    assert out == {"status": "skipped", "reason": "concurrent_execution"}


@pytest.mark.parametrize("bad", [
    {"runId": "x"},
    {"cursor": "not-an-arn"},
    {"depth": 999},
    {"lookbackStart": "yesterday"},
])
def test_invalid_continuation_rejected(rec, bad):
    out = rec.lambda_handler(_payload(**bad), Ctx(100))
    assert out == {"status": "skipped", "reason": "invalid_continuation"}
    rec._config_table.put_item.assert_not_called()


def test_failed_handoff_releases_guard_and_records_error(rec):
    _events(rec, 4)
    rec._process_single_event = lambda e, c, n: "ingested"
    rec._lambda_client.invoke.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "Invoke")
    out = rec.lambda_handler({"detail-type": "Scheduled Event"}, Ctx(2))
    assert out["status"] == "completed_with_errors"
    final = rec._config_table.put_item.call_args.kwargs["Item"]
    assert final["status"] == "completed_with_errors"
    assert final["error_details"][-1]["errorCode"] == "SYS_RECONCILIATION_CONTINUATION_FAILED"


def test_continuation_limit_stops_chain(rec):
    _events(rec, 4)
    rec._process_single_event = lambda e, c, n: "ingested"
    # depth is incremented on entry, so MAX-1 -> MAX: no further hops allowed
    out = rec.lambda_handler(_payload(depth=rec._MAX_CONTINUATIONS - 1), Ctx(0))
    assert out["status"] == "completed_with_errors"
    rec._lambda_client.invoke.assert_not_called()
