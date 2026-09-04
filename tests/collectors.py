"""The root collectors and the gateway, loaded by path.

``cc-usage.py``, ``codex-usage.py`` and ``usage-dashboard.py`` carry a hyphen so
that they read as commands: no ``import`` statement can name them.  They are
loaded once here rather than through the same loader repeated in every test
module.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests import SOURCE


FIXTURES = Path(__file__).parent / "fixtures" / "codex"
CLAUDE_FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cc_usage = _load("cc_usage", "cc-usage.py")
codex_usage = _load("codex_usage", "codex-usage.py")
usage_dashboard = _load("usage_dashboard", "usage-dashboard.py")
