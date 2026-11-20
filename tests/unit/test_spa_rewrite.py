"""The dashboard's CloudFront Function must route app paths to index.html only."""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
FN = os.path.join(ROOT, "stacks", "cloudfront", "spa_rewrite.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("uri,expected", [
    ("/", "/index.html"),
    ("/campaigns", "/index.html"),
    ("/routing/health", "/index.html"),
    ("/index.html", "/index.html"),
    ("/assets/index-C2YK9lKM.js", "/assets/index-C2YK9lKM.js"),   # missing -> real 404
    ("/assets/index-C-i3T3uC.css", "/assets/index-C-i3T3uC.css"),
    ("/config.json", "/config.json"),
    ("/v1.2/page", "/index.html"),                                 # dot in a directory only
])
def test_rewrite(uri, expected):
    script = open(FN).read() + f"\nconsole.log(handler({{request: {{uri: {json.dumps(uri)}}}}}).uri);"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == expected
