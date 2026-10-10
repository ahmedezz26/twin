"""deploy.py copies a fixed list of modules into the Lambda zip. A module missing from that list
would only fail on Lambda, after a deploy. This test catches it on the laptop instead."""

from pathlib import Path

import deploy

BACKEND_DIR = Path(__file__).parent.parent


def test_every_backend_module_is_packaged_for_lambda():
    modules = {path.name for path in BACKEND_DIR.glob("*.py")} - {"deploy.py"}

    assert modules == set(deploy.APP_FILES)
