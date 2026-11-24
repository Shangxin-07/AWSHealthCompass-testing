"""-c event_capture modes synthesize the right capture rules and queue policy."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
pytest.importorskip("aws_cdk")
BASE = {"account": "123456789012", "ops_alert_email": "ci@example.com"}


def _synth(extra):
    dist = os.path.join(ROOT, "dashboard", "dist")
    made = not os.path.isdir(dist)
    if made:
        os.makedirs(dist)
        open(os.path.join(dist, "index.html"), "w").write("test")
    out = tempfile.mkdtemp()
    try:
        env = {**os.environ, "CDK_OUTDIR": out, "CDK_CONTEXT_JSON": json.dumps({**BASE, **extra})}
        subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        templates = {os.path.basename(f)[:-len(".template.json")]: json.load(open(f))
                     for f in glob.glob(os.path.join(out, "*.template.json"))}
        manifest = json.load(open(os.path.join(out, "manifest.json")))["artifacts"]
        regions = {k: v["environment"].rsplit("/", 1)[1] for k, v in manifest.items()
                   if v.get("type") == "aws:cloudformation:stack"}
        return templates, regions
    finally:
        shutil.rmtree(out, ignore_errors=True)
        if made:
            shutil.rmtree(dist, ignore_errors=True)


def _health_rules(templates):
    return sorted(name for name, t in templates.items() for r in t["Resources"].values()
                  if r["Type"] == "AWS::Events::Rule" and "aws.health" in json.dumps(r["Properties"].get("EventPattern", {})))


def _queue_policy_sources(templates):
    out = []
    for r in templates["CompassCore"]["Resources"].values():
        if r["Type"] == "AWS::SQS::QueuePolicy":
            for st in r["Properties"]["PolicyDocument"]["Statement"]:
                arn = st.get("Condition", {}).get("ArnEquals", {}).get("aws:SourceArn")
                if arn:
                    out.append(arn if isinstance(arn, str) else json.dumps(arn))
    return out


def test_default_is_simplified_us_west_2():
    t, regions = _synth({})
    assert regions["CompassEventCapture"] == "us-west-2"
    assert _health_rules(t) == ["CompassEventCapture"]
    assert [s for s in _queue_policy_sources(t) if "us-west-2" in s]


def test_regional_uses_primary_and_us_east_1_without_us_west_2():
    t, regions = _synth({"primary_region": "ap-southeast-2", "event_capture": "regional", "deploy_test_tools": "true"})
    assert regions["CompassEventCapture-ap-southeast-2"] == "ap-southeast-2"
    assert regions["CompassEventCapture-us-east-1"] == "us-east-1"
    assert "us-west-2" not in regions.values()
    # No duplicate test-tools Health rule when the primary Region is captured.
    assert _health_rules(t) == ["CompassEventCapture-ap-southeast-2", "CompassEventCapture-us-east-1"]
    sources = _queue_policy_sources(t)
    assert any("ap-southeast-2" in s and "compass-health-event-capture" in s for s in sources)
    assert any("us-east-1" in s and "compass-health-event-capture" in s for s in sources)
    assert not any("us-west-2" in s for s in sources)
    # Same-Region capture stack reuses CoreStack's alarm topic (names would collide).
    assert not any(r["Type"] == "AWS::SNS::Topic"
                   for r in t["CompassEventCapture-ap-southeast-2"]["Resources"].values())
    assert any(r["Type"] == "AWS::SNS::Topic" for r in t["CompassEventCapture-us-east-1"]["Resources"].values())


def test_none_and_legacy_flag_have_no_capture_rules():
    for extra in ({"event_capture": "none"}, {"deploy_event_capture": "false"}):
        t, regions = _synth({"primary_region": "ap-southeast-2", **extra})
        assert _health_rules(t) == [] and not _queue_policy_sources(t)


def test_invalid_mode_rejected():
    with pytest.raises(subprocess.CalledProcessError):
        _synth({"event_capture": "everywhere"})
