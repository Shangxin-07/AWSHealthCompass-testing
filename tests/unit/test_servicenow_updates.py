"""ServiceNow: one record per account per campaign, updated as Health reports progress."""
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
moto = pytest.importorskip("moto")
import boto3  # noqa: E402

CID = "RDS:AWS_RDS_PLANNED_LIFECYCLE_EVENT"
A, B = "600000000001", "600000000005"


class FakeSnow:
    def __init__(self):
        self.created, self.updates, self.notes, self.n = [], [], [], 0

    def create_ticket(self, req):
        self.n += 1
        self.created.append(req)
        return SimpleNamespace(ticket_id=f"CHG00{self.n:05d}", ticket_url=f"https://x.service-now.com/{self.n}",
                               raw_response={"sys_id": f"sys{self.n}"})

    def update_ticket(self, ref, fields):
        self.updates.append((ref, fields))

    def add_work_note(self, ref, note):
        self.notes.append((ref, note))


@pytest.fixture()
def env(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                 "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        s = lambda n: {"AttributeName": n, "AttributeType": "S"}
        res = r.create_table(TableName="compass-resources", BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "campaignId", "KeyType": "HASH"},
                                        {"AttributeName": "trackingKey", "KeyType": "RANGE"}],
                             AttributeDefinitions=[s("campaignId"), s("trackingKey")])
        r.create_table(TableName="compass-config", BillingMode="PAY_PER_REQUEST",
                       KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}], AttributeDefinitions=[s("pk")])
        for acct in (A, B):
            for i in range(3):
                v = f"arn:aws:rds:us-east-1:{acct}:db:d{i}"
                res.put_item(Item={"campaignId": CID, "trackingKey": v, "entityValue": v, "accountId": acct,
                                   "healthStatus": "PENDING", "ticketStatus": "none", "ticketId": "", "tickets": {}})
        sys.path.insert(0, os.path.join(ROOT, "lambdas/servicenow_integration"))
        spec = importlib.util.spec_from_file_location("snow_t", os.path.join(ROOT, "lambdas/servicenow_integration/handler.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        yield m, res, FakeSnow()


TARGET = {"assignmentGroupId": "grp1", "recordType": "change_request"}


def _payload(acct, action, newly=()):
    return {"event": {"campaignId": CID, "affectedAccount": acct, "service": "RDS",
                      "eventTypeCode": "AWS_RDS_PLANNED_LIFECYCLE_EVENT", "action": action, "accountTags": {}},
            "resources": [{"entityValue": f"arn:aws:rds:us-east-1:{acct}:db:d{i}"} for i in range(3)],
            "routing": {"accountName": "lt-finance-prod", "platforms": {"servicenow": TARGET}},
            "newlyResolved": list(newly)}


def _rows(res, acct):
    return [r for r in res.query(KeyConditionExpression="campaignId = :c",
                                 ExpressionAttributeValues={":c": CID})["Items"] if r.get("accountId") == acct]


def test_create_records_ticket_on_the_accounts_rows(env):
    m, res, snow = env
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    assert len(snow.created) == 1
    rows = _rows(res, A)
    assert {r["ticketId"] for r in rows} == {"CHG0000001"}
    assert {r["tickets"]["servicenow"]["ticketRef"] for r in rows} == {"change_request/sys1"}
    assert all(r["ticketPlatform"] == "servicenow" for r in rows)
    assert {r.get("ticketId") for r in _rows(res, B)} == {""}            # other account untouched
    all_keys = [r["trackingKey"] for r in res.scan()["Items"]]
    assert not any(k.startswith("TICKET#") for k in all_keys)           # no separate tracking row


def test_repeat_create_does_not_duplicate(env):
    m, res, snow = env
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    m._process_event(snow, _payload(A, "RECONCILE"), TARGET)
    assert len(snow.created) == 1


def test_first_event_for_account_in_existing_campaign_creates(env):
    m, res, snow = env
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    m._process_event(snow, _payload(B, "RESOURCE_UPDATE"), TARGET)    # merged campaign: B's first event
    assert len(snow.created) == 2 and snow.updates == []
    assert {r["ticketId"] for r in _rows(res, B)} == {"CHG0000002"}


def test_resource_update_updates_title_and_adds_work_note(env):
    m, res, snow = env
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    for i in (0, 1):   # Processor has already written the new statuses
        res.update_item(Key={"campaignId": CID, "trackingKey": f"arn:aws:rds:us-east-1:{A}:db:d{i}"},
                        UpdateExpression="SET healthStatus = :r", ExpressionAttributeValues={":r": "RESOLVED"})
    newly = [f"arn:aws:rds:us-east-1:{A}:db:d0", f"arn:aws:rds:us-east-1:{A}:db:d1",
             f"arn:aws:rds:us-east-1:{B}:db:d0"]                        # B's isn't A's ticket
    m._process_event(snow, _payload(A, "RESOURCE_UPDATE", newly), TARGET)
    assert len(snow.created) == 1
    ref, fields = snow.updates[0]
    assert ref == "change_request/sys1"
    assert "600000000001 (lt-finance-prod)" in fields["short_description"]
    assert "(1 pending, 2 resolved)" in fields["short_description"]
    note = snow.notes[0][1]
    assert "2 resource(s) resolved" in note and "db:d0" in note and "db:d1" in note
    assert B not in note and "Remaining pending: 1 of 3" in note


def test_update_without_newly_resolved_has_no_work_note(env):
    m, res, snow = env
    m._process_event(snow, _payload(A, "CREATE"), TARGET)
    m._process_event(snow, _payload(A, "RESOURCE_UPDATE"), TARGET)
    assert len(snow.updates) == 1 and snow.notes == []


def test_never_creates_rows_for_unknown_accounts(env):
    m, res, snow = env
    m._process_event(snow, _payload("600000000009", "CREATE"), TARGET)
    assert len(snow.created) == 1                                        # ticket still raised
    assert _rows(res, "600000000009") == []                              # but no bare rows
