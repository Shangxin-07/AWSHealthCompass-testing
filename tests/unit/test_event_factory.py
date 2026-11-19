"""The synthetic event factory must produce events the Processor can parse."""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))

from resolve_core.event_parser import (  # noqa: E402
    coerce_page_fields, extract_entities, infer_actionability, parse_health_date,
)


def _factory():
    spec = importlib.util.spec_from_file_location(
        "event_factory", os.path.join(ROOT, "lambdas/event_generator/event_factory.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_event_parses_with_processor_helpers():
    f = _factory()
    ev = f.make_health_event(service="rds", affected_account="123456789012", resource_count=4)
    d = ev["detail"]
    assert ev["source"] == "aws.health"
    assert d["eventArn"].startswith("arn:aws:health:us-east-1::event/RDS/")
    assert len(extract_entities(d)) == 4
    assert parse_health_date(d["startTime"]).endswith("Z")
    assert infer_actionability(d).value == "ACTION_REQUIRED"
    page = coerce_page_fields(d)
    assert (page.page, page.total_pages) == (1, 1)


def test_unique_arns_and_overrides():
    f = _factory()
    a, b = f.make_health_event(), f.make_health_event()
    assert a["detail"]["eventArn"] != b["detail"]["eventArn"]
    c = f.make_health_event(page="2", total_pages="3", detail={"eventArn": a["detail"]["eventArn"]})
    assert c["detail"]["eventArn"] == a["detail"]["eventArn"]
    assert coerce_page_fields(c["detail"]).page == 2


def test_resources_variants():
    f = _factory()
    bare = f.make_resources(2, service="EC2", bare_ids=True)
    assert all(r["entityValue"].startswith("i-") for r in bare)
    tagged = f.make_resources(1, service="EKS", account="999999999999", tags={"Team": "x"})
    assert tagged[0]["resourceTags"] == {"Team": "x"}
    assert ":999999999999:" in tagged[0]["entityValue"]
    assert f.make_health_event(resource_count=0)["detail"]["affectedEntities"] == []
