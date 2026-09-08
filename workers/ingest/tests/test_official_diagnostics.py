"""Pure contracts for public-safe official failure diagnoses."""
from __future__ import annotations

import pytest

from billcommons_ingest import official_ca_actions as ca_actions
from billcommons_ingest import official_diagnostics as diagnostics


def test_known_capture_error_has_only_allowlisted_numeric_details():
    error = ca_actions.OfficialCaActionsError(
        "upstream supplied private text that must not enter scope",
        code="http_status_unexpected",
        http_status=503,
    )

    assert diagnostics.failure_diagnosis(error, stage="capture") == {
        "version": 1,
        "stage": "capture",
        "code": "http_status_unexpected",
        "recommended_action": "retry_as_scheduled",
        "details": {"http_status": 503},
    }


def test_legacy_source_contract_and_unknown_error_never_copy_exception_text():
    legacy = ca_actions.OfficialCaActionsError("private URL and token text")
    unknown = RuntimeError("private URL and token text")

    source_contract = diagnostics.failure_diagnosis(legacy, stage="parse")
    adapter_failure = diagnostics.failure_diagnosis(unknown, stage="replay")

    assert source_contract == {
        "version": 1,
        "stage": "parse",
        "code": "source_contract_failure",
        "recommended_action": "review_source_schema",
    }
    assert adapter_failure == {
        "version": 1,
        "stage": "replay",
        "code": "adapter_failure",
        "recommended_action": "inspect_adapter_failure",
    }
    assert "private" not in repr(source_contract)
    assert "private" not in repr(adapter_failure)


@pytest.mark.parametrize(
    ("code", "details"),
    [
        ("not_allowlisted", None),
        ("http_status_unexpected", {"member_name": 1}),
        ("http_status_unexpected", {"observed": True}),
        ("http_status_unexpected", {"observed": diagnostics.MAX_SAFE_NUMERIC + 1}),
    ],
)
def test_error_metadata_rejects_unbounded_or_nonpublic_values(code, details):
    with pytest.raises((TypeError, ValueError)):
        ca_actions.OfficialCaActionsError("compatibility message", code=code, details=details)
