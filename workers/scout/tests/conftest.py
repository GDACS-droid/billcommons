"""Allow direct ``pytest workers/scout/tests`` before the optional worker is installed.

Production uses the package/Scout Dockerfile; this only mirrors the existing
source-tree test convention and avoids requiring a hand-written PYTHONPATH.
"""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
