"""dispatch_webhooks.py is a standalone script (like workers/alerts/
send_alerts.py), not an installed package -- there is no pyproject.toml here
to add it to sys.path via an editable install. Add workers/webhooks/ to
sys.path directly, the same shape workers/alerts would need if it ever grew
a test suite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
