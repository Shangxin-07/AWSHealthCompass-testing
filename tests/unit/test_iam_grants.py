"""Synthesize the app and check Lambda roles allow the DynamoDB calls their code makes.

Unit tests mock DynamoDB, so a missing IAM action only shows up at runtime.
This caught Sync's missing dynamodb:BatchGetItem (used by _batch_get_items).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
pytest.importorskip("aws_cdk")


@pytest.fixture(scope="module")
def api_template():
    dist = os.path.join(ROOT, "dashboard", "dist")
    created_dist = not os.path.isdir(dist)
    if created_dist:
        os.makedirs(dist)
        open(os.path.join(dist, "index.html"), "w").write("test")
    out = tempfile.mkdtemp()
    try:
        env = {**os.environ, "CDK_OUTDIR": out, "CDK_CONTEXT_JSON": json.dumps(
            {"account": "123456789012", "ops_alert_email": "ci@example.com"})}
        subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        with open(os.path.join(out, "CompassApi.template.json")) as f:
            yield json.load(f)
    finally:
        shutil.rmtree(out, ignore_errors=True)
        if created_dist:
            shutil.rmtree(dist, ignore_errors=True)


def _actions_for_role(template, role_logical_prefix):
    actions = set()
    for res in template["Resources"].values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        roles = json.dumps(res["Properties"].get("Roles", []))
        if role_logical_prefix not in roles:
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            a = st["Action"]
            actions.update(a if isinstance(a, list) else [a])
    return actions


def test_sync_role_can_build_ticket_index(api_template):
    actions = _actions_for_role(api_template, "SyncLambdaServiceRole")
    assert {"dynamodb:Query", "dynamodb:BatchGetItem"} <= actions, sorted(actions)


def _resources_for_role(template, role_logical_prefix):
    out = []
    for res in template["Resources"].values():
        if res["Type"] == "AWS::IAM::Policy" and role_logical_prefix in json.dumps(res["Properties"].get("Roles", [])):
            out.append(json.dumps(res["Properties"]["PolicyDocument"]))
    return "\n".join(out)


@pytest.mark.parametrize("role", ["SyncLambdaServiceRole", "ReconciliationLambdaServiceRole", "ApiLambdaServiceRole"])
def test_stats_table_access(api_template, role):
    actions = _actions_for_role(api_template, role)
    assert {"dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"} <= actions, sorted(actions)
    assert "CampaignStatsTable" in _resources_for_role(api_template, role)
