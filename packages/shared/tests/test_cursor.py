"""billcommons_shared.cursor -- the encoding shared by /changes and the
webhook dispatcher's delivery payload `cursor` field (see that module's
docstring for why the two callers can't drift)."""
from __future__ import annotations

import pytest

from billcommons_shared.cursor import InvalidCursor, decode_cursor, encode_cursor


def test_round_trips():
    assert decode_cursor(encode_cursor(0)) == 0
    assert decode_cursor(encode_cursor(123456789)) == 123456789


def test_garbage_raises_invalid_cursor():
    with pytest.raises(InvalidCursor):
        decode_cursor("not a real cursor")


def test_wrong_version_raises_invalid_cursor():
    import base64
    import json

    raw = json.dumps({"v": 99, "seq": 1}, separators=(",", ":"))
    bogus = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(bogus)


# ---------------------------------------------------------------------------
# Round-2 fix #10: a non-dict JSON payload (base64("[]"), base64("123"),
# base64("null")) must raise InvalidCursor (-> 400 at the API), not
# AttributeError (-> 500) from calling `.get` on a list/int/None.
# ---------------------------------------------------------------------------


def _b64(raw_json: str) -> str:
    import base64

    return base64.urlsafe_b64encode(raw_json.encode()).decode().rstrip("=")


@pytest.mark.parametrize("raw_json", ["[]", "123", "null", '"a string"', "true"])
def test_non_dict_json_payload_raises_invalid_cursor_not_attribute_error(raw_json):
    with pytest.raises(InvalidCursor):
        decode_cursor(_b64(raw_json))


@pytest.mark.parametrize("bad_seq", ["true", "false", "1.9", '"5"', "null"])
def test_non_int_seq_raises_invalid_cursor(bad_seq):
    import json

    raw = json.dumps({"v": 1, "seq": json.loads(bad_seq)}, separators=(",", ":"))
    with pytest.raises(InvalidCursor):
        decode_cursor(_b64(raw))


# ---------------------------------------------------------------------------
# Round-3 fix #7: seq must be within Postgres' signed-bigint range, or a
# `WHERE seq > :cursor_seq` bind on /api/v1/changes overflows at the SQL
# layer -- a 500 instead of the 400 InvalidCursor every other malformed
# cursor already gets.
# ---------------------------------------------------------------------------


def test_negative_seq_raises_invalid_cursor():
    import json

    raw = json.dumps({"v": 1, "seq": -1}, separators=(",", ":"))
    with pytest.raises(InvalidCursor):
        decode_cursor(_b64(raw))


def test_seq_beyond_bigint_max_raises_invalid_cursor():
    import json

    raw = json.dumps({"v": 1, "seq": 2**63}, separators=(",", ":"))  # one past bigint max
    with pytest.raises(InvalidCursor):
        decode_cursor(_b64(raw))


def test_seq_at_bigint_max_is_accepted():
    assert decode_cursor(encode_cursor(2**63 - 1)) == 2**63 - 1


def test_seq_zero_is_accepted():
    """0 is a legitimate watermark value (see webhook_subscriptions.last_seq's
    own "0 if none" rule) -- the bigint-range guard must reject NEGATIVE
    values, not merely non-positive ones."""
    assert decode_cursor(encode_cursor(0)) == 0
