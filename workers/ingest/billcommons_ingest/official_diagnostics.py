"""Safe, bounded failure diagnoses for official-source observations.

The values returned here are deliberately fit for durable observation scope:
they describe a known failure category without copying exception text, HTTP
headers, URLs, archive member names, or response bytes.  Recommendations are
advisory labels only; this module never changes scheduling, targets, limits,
or source access.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


VERSION = 1
STAGES = frozenset({"capture", "parse", "evidence_validation", "replay"})
RECOMMENDED_ACTIONS = frozenset(
    {
        "retry_as_scheduled",
        "review_source_endpoint",
        "review_capture_limit",
        "review_parser_limit",
        "review_source_schema",
        "inspect_adapter_failure",
    }
)
MAX_SAFE_NUMERIC = (1 << 63) - 1
SAFE_DETAIL_KEYS = frozenset({"http_status", "observed", "limit"})

# Codes are stable machine values.  Keep this list intentionally small: a
# diagnostic must not become an alternate channel for upstream text.
CODE_ACTIONS = {
    "http_status_unexpected": "review_source_endpoint",
    "unexpected_response_url": "review_source_endpoint",
    "invalid_content_length": "review_source_endpoint",
    "content_length_limit_exceeded": "review_capture_limit",
    "response_chunk_limit_exceeded": "review_capture_limit",
    "response_size_limit_exceeded": "review_capture_limit",
    "response_deadline_exceeded": "retry_as_scheduled",
    "parser_response_size_limit_exceeded": "review_parser_limit",
    "archive_member_count_limit_exceeded": "review_parser_limit",
    "archive_duplicate_member_names": "review_source_schema",
    "archive_missing_required_tables": "review_source_schema",
    "archive_encrypted_member": "review_source_schema",
    "archive_member_size_limit_exceeded": "review_parser_limit",
    "archive_total_size_limit_exceeded": "review_parser_limit",
    "archive_invalid_compressed_size": "review_source_schema",
    "archive_compression_ratio_limit_exceeded": "review_parser_limit",
    "archive_member_size_changed": "review_source_schema",
    "archive_crc_or_invalid_zip": "review_source_schema",
    "observation_deadline_exceeded": "retry_as_scheduled",
}
ALLOWED_CODES = frozenset(CODE_ACTIONS) | {"source_contract_failure", "adapter_failure"}

_GENERIC_ACTIONS = {
    "capture": "review_source_endpoint",
    "parse": "review_source_schema",
    "evidence_validation": "inspect_adapter_failure",
    "replay": "inspect_adapter_failure",
}


def validate_error_metadata(
    code: str | None, details: Mapping[str, int] | None,
) -> tuple[str | None, dict[str, int]]:
    """Validate optional error metadata before it can reach durable scope."""

    if code is not None and code not in ALLOWED_CODES:
        raise ValueError("unsupported official diagnosis code")
    if details is None:
        return code, {}
    if not isinstance(details, Mapping):
        raise TypeError("official diagnosis details must be a mapping")
    safe: dict[str, int] = {}
    for key, value in details.items():
        if key not in SAFE_DETAIL_KEYS:
            raise ValueError("unsupported official diagnosis detail")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SAFE_NUMERIC:
            raise ValueError("official diagnosis detail must be a bounded non-negative integer")
        safe[key] = value
    return code, safe


def _metadata_from_error(error: BaseException) -> tuple[str | None, dict[str, int]]:
    """Read only validated optional metadata; malformed attributes are ignored."""

    try:
        return validate_error_metadata(
            getattr(error, "diagnostic_code", None),
            getattr(error, "diagnostic_details", None),
        )
    except (AttributeError, TypeError, ValueError):
        return None, {}


def failure_diagnosis(error: BaseException, *, stage: str) -> dict[str, Any]:
    """Return a versioned, public-safe diagnosis for one handled failure."""

    if stage not in STAGES:
        raise ValueError("unsupported official diagnosis stage")
    code, details = _metadata_from_error(error)
    if code in CODE_ACTIONS:
        recommended_action = CODE_ACTIONS[code]
        # A bounded status is transport evidence, not endpoint-policy drift:
        # retain the normal failure cadence for rate limits and server errors.
        if code == "http_status_unexpected" and (
            details.get("http_status") == 429 or details.get("http_status", 0) >= 500
        ):
            recommended_action = "retry_as_scheduled"
    elif code == "source_contract_failure" or (code is None and type(error).__name__ == "OfficialCaActionsError"):
        # Old callers may construct this exception with arbitrary private text.
        # Its text is deliberately neither inspected nor persisted.
        code = "source_contract_failure"
        recommended_action = _GENERIC_ACTIONS[stage]
    elif code is None and type(error).__name__ == "ObservationDeadlineExceeded":
        code = "observation_deadline_exceeded"
        recommended_action = CODE_ACTIONS[code]
    else:
        code = "adapter_failure"
        recommended_action = "inspect_adapter_failure"

    result: dict[str, Any] = {
        "version": VERSION,
        "stage": stage,
        "code": code,
        "recommended_action": recommended_action,
    }
    if details:
        result["details"] = details
    return result
