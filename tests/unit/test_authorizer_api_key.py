"""Authorizer API-key check: value read at runtime, cached, rotation-aware, fail closed."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def auth(monkeypatch):
    monkeypatch.setenv("API_KEY_ID", "abc123")
    monkeypatch.setenv("USER_POOL_ID", "ap-southeast-2_x")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    spec = importlib.util.spec_from_file_location("authz_t", os.path.join(ROOT, "lambdas/authorizer/handler.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    state = {"value": "key-one", "calls": 0, "fail": False, "now": 1_000_000.0}

    def fetch():
        state["calls"] += 1
        return None if state["fail"] else state["value"]
    monkeypatch.setattr(m, "_fetch_api_key_value", fetch)
    monkeypatch.setattr(m.time, "time", lambda: state["now"])
    return m, state


def test_valid_and_invalid_keys_and_cache(auth):
    m, st = auth
    assert m._validate_api_key("key-one") is True
    assert m._validate_api_key("key-one") is True
    assert st["calls"] == 1                       # cached
    assert m._validate_api_key("") is False
    assert m._validate_api_key("wrong") is False
    assert st["calls"] == 1                       # miss refresh rate-limited (just fetched)


def test_rotated_key_accepted_after_miss_refresh_interval(auth):
    m, st = auth
    assert m._validate_api_key("key-one")
    st["value"] = "key-two"                       # rotated in API Gateway
    st["now"] += 30
    assert m._validate_api_key("key-two") is False   # within 60s: no refetch
    st["now"] += 31
    assert m._validate_api_key("key-two") is True    # refetched on miss
    assert m._validate_api_key("key-one") is False   # old key no longer accepted


def test_ttl_refresh(auth):
    m, st = auth
    m._validate_api_key("key-one")
    st["now"] += m._API_KEY_TTL_S
    m._validate_api_key("key-one")
    assert st["calls"] == 2


def test_fails_closed_when_lookup_fails(auth):
    m, st = auth
    st["fail"] = True
    assert m._validate_api_key("key-one") is False


def test_value_not_in_environment(auth):
    m, _ = auth
    assert not hasattr(m, "API_KEY_VALUE") and m.API_KEY_ID == "abc123"
