"""Load from Organizations explains the real cause of AccessDenied."""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def test_access_denied_points_to_management_account_and_csv():
    sys.path.insert(0, os.path.join(ROOT, "lambdas/shared/python"))
    sys.path.insert(0, os.path.join(ROOT, "lambdas/api"))
    spec = importlib.util.spec_from_file_location("rh_t", os.path.join(ROOT, "lambdas/api/routing_handlers.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    pager = MagicMock()
    pager.paginate.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ListAccounts")
    m._orgs = MagicMock(get_paginator=lambda _n: pager)
    out = m.handle_routing_discover({}, None)
    assert out["statusCode"] == 403
    msg = json.loads(out["body"])["error"]["message"]
    assert "management account" in msg and "delegated administrator" in msg and "Bulk Import" in msg
    assert "Lambda does not have permission" not in msg
