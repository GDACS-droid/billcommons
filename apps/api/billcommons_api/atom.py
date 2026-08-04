"""Atom 1.0 feed builder for GET /feeds/{jurisdiction}.atom.

Stdlib xml.etree.ElementTree only, deliberately -- not string formatting.
Bill titles and event `detail` text come verbatim from upstream jurisdiction
sources (see billcommons_api.search's "Security note" on the same class of
data) and are UNTRUSTED plain text, never sanitized HTML. ElementTree's
serializer escapes element text/attribute content (&, <, >, quotes)
automatically on `tostring()`, so a bill titled with a literal "&", "<", or a
CDATA-breaking sequence can never corrupt the feed's XML structure or, worse,
get interpreted as markup by a reader's feed client.

That escaping is NOT the whole guarantee, though: XML 1.0 forbids most C0
control characters in element content outright (only tab/LF/CR are legal),
and ElementTree passes them through byte-for-byte -- it escapes markup
characters, not illegal ones. A single stray \\x01 or \\x0c scraped into a
motion_text or bill title (real upstream CSV exports are not clean) would
make `ET.tostring()` emit bytes that are not well-formed XML at all, so the
FEED READER (or, ironically, our own tests' `ET.fromstring`) fails to parse
a 200 response. Two more failure modes in the same family, both reachable
from an ordinary Python `str` (no encoding trickery required, e.g. a
mis-decoded upstream byte stream landing a lone surrogate): a LONE
SURROGATE (U+D800-U+DFFF) makes `ET.tostring()` itself raise
`UnicodeEncodeError` -- the request 500s outright rather than serving a
malformed 200 -- and the NONCHARACTERS U+FFFE/U+FFFF serialize without
error but a conforming XML parser is required to reject them, so the
response is again a 200 nothing downstream can actually consume.
`_strip_illegal_xml_chars` runs on every string that lands in element text
below to close off all three at once.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from billcommons_schema.models import Bill, BillEvent, Jurisdiction

ATOM_NS = "http://www.w3.org/2005/Atom"
WEB_BASE = "https://billcommons.org"

# XML 1.0's legal character ranges exclude most C0 controls (only tab \x09,
# LF \x0a, CR \x0d survive), lone surrogates U+D800-U+DFFF, and the
# noncharacters U+FFFE/U+FFFF. \x7f (DEL) is technically INSIDE XML 1.0's
# legal range (#x20-#xD7FF is legal, and #x7F sits well within it) -- it is
# stripped here anyway not because the spec forbids it, but because it is
# scrape noise with no content value, same as the other bytes in this class.
_ILLEGAL_XML_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")


def _strip_illegal_xml_chars(value: str) -> str:
    return _ILLEGAL_XML_CHARS_RE.sub("", value)


# Fixed per the tag: URI scheme (RFC 4151): the date component records when
# this naming authority (billcommons.org) asserted control of the identifier
# space below it, NOT when any individual entry was created -- entries are
# already unique via the event's own `seq` (bill_events' gapless cursor
# column, see routers/changes.py), so this date never needs to change.
_TAG_AUTHORITY_DATE = "2026-08-04"


def _iso(dt: datetime | None) -> str:
    """RFC 3339 timestamp for Atom's <updated>. Falls back to "now" only for
    the feed-level element on a genuinely empty feed, where there is no real
    entry to derive a timestamp from -- see build_atom_feed."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = _strip_illegal_xml_chars(text)
    return el


def build_atom_feed(
    jurisdiction: Jurisdiction,
    rows: list[tuple[BillEvent, Bill]],
    self_url: str,
) -> bytes:
    """Render one jurisdiction's change events as an Atom 1.0 document.

    `rows` must already be ordered newest-first and already filtered to
    events past the caller's commit-safety-lag watermark -- this function
    does no filtering or ordering of its own, only rendering, so the
    safety-lag discipline lives in exactly one place (routers/changes.py's
    COMMIT_SAFETY_LAG_SECONDS, reused by routers/feeds.py).

    An empty `rows` list produces a fully valid feed with zero <entry>
    elements, per the repo's truth-in-emptiness rule (see emptiness.py):
    Atom natively supports a feed with no entries as a real, self-describing
    answer ("nothing changed recently in this jurisdiction"), distinct from
    an error -- it never needs an explicit disclosure the way an empty JSON
    array does, because the caller asked for a feed and got one.
    """
    feed = ET.Element("feed", {"xmlns": ATOM_NS})
    feed_id = f"tag:billcommons.org,{_TAG_AUTHORITY_DATE}:feeds/{jurisdiction.abbreviation}"
    _sub(feed, "id", feed_id)
    _sub(feed, "title", f"Bill Commons — {jurisdiction.name} change feed")
    # Feed-level <updated> = the newest entry's timestamp, or "now" when the
    # feed is legitimately empty (there is no entry to derive it from).
    #
    # max(changed_at), not rows[0][0].changed_at: `rows` is ordered by `seq`
    # DESC, and seq order is NOT changed_at order -- that exact skew (a
    # transaction can hold a lower seq that only becomes visible after
    # higher ones already committed) is the whole reason
    # COMMIT_SAFETY_LAG_SECONDS exists in routers/changes.py. Taking the
    # first row by seq would occasionally understate the feed's own
    # freshness by however wide that skew happened to be.
    newest_changed_at = max((event.changed_at for event, _bill in rows), default=None)
    _sub(feed, "updated", _iso(newest_changed_at))
    # RFC 4287 sec 4.1.1: a feed MUST have atom:author unless every entry
    # carries its own -- ours don't (there is no per-event byline, only a
    # bill/jurisdiction), so the feed-level one is required for a valid feed,
    # not decorative.
    author = _sub(feed, "author")
    _sub(author, "name", "Bill Commons")
    _sub(feed, "link", rel="self", href=self_url, type="application/atom+xml")
    _sub(
        feed,
        "link",
        rel="alternate",
        href=f"{WEB_BASE}/states/{jurisdiction.abbreviation}",
        type="text/html",
    )

    for event, bill in rows:
        entry = _sub(feed, "entry")
        entry_id = f"tag:billcommons.org,{_TAG_AUTHORITY_DATE}:bill_events/{event.seq}"
        _sub(entry, "id", entry_id)
        title = f"{jurisdiction.abbreviation} {bill.identifier} — {event.kind}"
        if event.detail:
            title += f": {event.detail}"
        _sub(entry, "title", title)
        _sub(entry, "updated", _iso(event.changed_at))
        _sub(entry, "link", href=f"{WEB_BASE}/bills/{bill.id}")
        if event.detail:
            _sub(entry, "summary", event.detail)

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)
