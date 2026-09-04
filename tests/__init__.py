"""Unit tests, run from the repository root.

The modules under test live in ``src/``, next to the dashboard template and the
stylesheet they inline.  This package puts that directory on the import path so
a test can name ``codex_log`` the way the collectors do.
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "src"

if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
