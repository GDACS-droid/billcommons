"""Narrow, bounded discovery for Florida Senate committee meeting documents.

The Senate bill page, committee index, and meeting PDF are separate sources.
This module only establishes the two URL bindings and the limited textual
preconditions needed before the worker may retain and describe the PDF.  It
does not fetch URLs or infer a legislative outcome from a meeting record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

from billcommons_shared.scout import ScoutPolicyError, canonicalize_url


_SENATE_HOST = "www.flsenate.gov"
_BILL_PATH_RE = re.compile(r"^/Session/Bill/(?P<year>\d{4})/\d{1,6}(?:/.*)?$", re.I)
_COMMITTEE_PATH_RE = re.compile(r"^/Committees/Show/(?P<code>[A-Z0-9]{1,12})$")
_DOCUMENT_PATH_RE = re.compile(
    r"^/Committees/Show/(?P<code>[A-Z0-9]{1,12})/"
    r"(?P<kind>ExpandedAgenda|MeetingNotice)/(?P<document>[1-9][0-9]*)$",
    re.I,
)
_AGENDA_ACTION_RE = re.compile(
    r"\bOn\s+Committee\s+agenda\s*--\s*"
    r"(?P<label>.+?),\s*(?P<date>\d{1,2}/\d{1,2}/\d{2})\s*,",
    re.I,
)
_INDEX_CAPTION_RE = re.compile(r"^(?P<year>\d{4})\s+Meeting\s+Records$", re.I)
_ROW_DATE_RE = re.compile(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})$")
_SPACE_RE = re.compile(r"\s+")
_EVIDENCE_IDENTIFIER_RE = re.compile(
    r"^\s*(?P<chamber>H\.?\s*B\.?|S\.?\s*B\.?)\s*(?P<number>\d{1,6})\s*$",
    re.I,
)
def _normalized(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


@dataclass(frozen=True)
class MeetingRequest:
    """One bill-referenced, session-pinned Senate committee meeting index."""

    canonical_url: str
    session_year: int
    committee_code: str
    meeting_date: date


@dataclass(frozen=True)
class MeetingDocument:
    """One exact committee-meeting artifact selected from a pinned index."""

    canonical_url: str
    artifact_type: Literal["committee expanded agenda", "committee meeting notice"]
    session_year: int


class _ParentParser(HTMLParser):
    """Collect visible text and anchor labels from an already bounded page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self.text: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded in {"script", "style", "template", "noscript"}:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if folded == "a" and self._anchor_href is None:
            href = next((value for name, value in attrs if name.casefold() == "href"), None)
            if href:
                self._anchor_href = href
                self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in {"script", "style", "template", "noscript"}:
            if self._suppressed_depth:
                self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if folded == "a" and self._anchor_href is not None:
            label = _normalized(" ".join(self._anchor_text))
            if label:
                self.anchors.append((label, self._anchor_href))
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        self.text.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)


@dataclass
class _IndexRow:
    cells: list[list[str]]
    anchors: list[tuple[str, str]]


class _MeetingIndexParser(HTMLParser):
    """Parse only the target meetings table; other portal links are ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._target_depth = 0
        self._target_tables = 0
        self._nested_table = False
        self._caption_active = False
        self._caption_count = 0
        self._caption: list[str] = []
        self._row: _IndexRow | None = None
        self._cell: list[str] | None = None
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self.caption = ""
        self.rows: list[_IndexRow] = []

    @property
    def _in_target(self) -> bool:
        return self._target_depth > 0

    @property
    def valid(self) -> bool:
        return (
            self._target_depth == 0
            and self._target_tables == 1
            and self._caption_count == 1
            and not self._nested_table
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded == "table" and self._in_target:
            self._target_depth += 1
            self._nested_table = True
            return
        if folded == "table" and not self._in_target:
            table_id = next((value for name, value in attrs if name.casefold() == "id"), None)
            if table_id == "meetingsTbl":
                self._target_tables += 1
                self._target_depth = 1
        if not self._in_target:
            return
        if folded == "caption" and not self._caption_active:
            self._caption_active = True
            self._caption_count += 1
            self._caption = []
        elif folded == "tr" and self._row is None:
            self._row = _IndexRow(cells=[], anchors=[])
        elif folded == "td" and self._row is not None and self._cell is None:
            self._cell = []
            self._row.cells.append(self._cell)
        elif folded == "a" and self._row is not None and self._anchor_href is None:
            href = next((value for name, value in attrs if name.casefold() == "href"), None)
            if href:
                self._anchor_href = href
                self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if self._in_target:
            if folded == "a" and self._anchor_href is not None:
                label = _normalized(" ".join(self._anchor_text))
                if label and self._row is not None:
                    self._row.anchors.append((label, self._anchor_href))
                self._anchor_href = None
                self._anchor_text = []
            elif folded == "td" and self._cell is not None:
                self._cell = None
            elif folded == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None
            elif folded == "caption" and self._caption_active:
                self.caption = _normalized(" ".join(self._caption))
                self._caption_active = False
            if folded == "table":
                self._target_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._in_target:
            return
        if self._caption_active:
            self._caption.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)


def _parse_html(parser: HTMLParser, body: bytes, maximum: int) -> bool:
    if maximum <= 0 or len(body) > maximum:
        return False
    try:
        parser.feed(body.decode("utf-8", "replace"))
        parser.close()
    except Exception:
        return False
    return True


def _senate_parent_url(parent_url: str) -> tuple[str, int] | None:
    try:
        canonical = canonicalize_url(parent_url)
    except ScoutPolicyError:
        return None
    parts = urlsplit(canonical)
    if parts.hostname != _SENATE_HOST:
        return None
    match = _BILL_PATH_RE.fullmatch(parts.path)
    if match is None:
        return None
    return canonical, int(match.group("year"))


def _committee_anchor_url(parent_url: str, href: str) -> tuple[str, str] | None:
    if not _safe_href(href):
        return None
    candidate = urljoin(parent_url, href)
    raw = urlsplit(candidate)
    if raw.query or raw.fragment:
        return None
    try:
        canonical = canonicalize_url(candidate)
    except ScoutPolicyError:
        return None
    parts = urlsplit(canonical)
    if parts.hostname != _SENATE_HOST or parts.query:
        return None
    match = _COMMITTEE_PATH_RE.fullmatch(parts.path)
    if match is None:
        return None
    return canonical, match.group("code")


def _safe_href(href: str) -> bool:
    """Reject route-changing raw syntax before ``urljoin`` can normalize it."""
    if not isinstance(href, str) or not href or len(href) > 4096 or "%" in href or "\\" in href:
        return False
    try:
        parts = urlsplit(href)
    except ValueError:
        return False
    if parts.query or parts.fragment:
        return False
    return all(part not in {".", ".."} for part in parts.path.split("/"))


def discover_meeting_request(
    parent_url: str,
    body: bytes,
    *,
    max_html_bytes: int = 256 * 1024,
) -> MeetingRequest | None:
    """Bind the latest bill-referenced agenda to its exact committee index.

    A committee name in history alone is insufficient.  The same bounded bill
    page must contain the matching official committee anchor, and the result
    pins that committee route to the bill's regular-session year.
    """
    parent = _senate_parent_url(parent_url)
    if parent is None:
        return None
    canonical_parent, session_year = parent
    parser = _ParentParser()
    if not _parse_html(parser, body, max_html_bytes):
        return None

    committee_codes: dict[str, str] = {}
    ambiguous_labels: set[str] = set()
    for label, href in parser.anchors:
        anchor = _committee_anchor_url(canonical_parent, href)
        if anchor is None:
            continue
        _, code = anchor
        label_key = _normalized(label).casefold()
        if label_key in ambiguous_labels:
            continue
        prior = committee_codes.get(label_key)
        # A label resolving to different committee codes is source ambiguity,
        # not a reason to pick one route.
        if prior is None:
            committee_codes[label_key] = code
        elif prior != code:
            committee_codes.pop(label_key, None)
            ambiguous_labels.add(label_key)

    candidates: list[tuple[date, str]] = []
    visible_text = _normalized(" ".join(parser.text))
    for match in _AGENDA_ACTION_RE.finditer(visible_text):
        label = _normalized(match.group("label")).casefold()
        code = committee_codes.get(label)
        if code is None:
            continue
        try:
            meeting_date = date.fromisoformat(
                f"20{match.group('date').rsplit('/', 1)[1]}-"
                f"{int(match.group('date').split('/', 1)[0]):02d}-"
                f"{int(match.group('date').split('/')[1]):02d}"
            )
        except ValueError:
            continue
        if meeting_date.year not in {session_year, session_year - 1}:
            continue
        candidates.append((meeting_date, code))
    if not candidates:
        return None
    meeting_date = max(item[0] for item in candidates)
    committee_codes_for_latest_date = {code for candidate_date, code in candidates if candidate_date == meeting_date}
    # A same-day agenda in two committees needs a meeting-time or another
    # source-level identity field.  Do not let lexicographic committee order
    # choose one and present it as the bill's latest meeting.
    if len(committee_codes_for_latest_date) != 1:
        return None
    committee_code = committee_codes_for_latest_date.pop()
    return MeetingRequest(
        canonical_url=f"https://{_SENATE_HOST}/Committees/Show/{committee_code}/{session_year}",
        session_year=session_year,
        committee_code=committee_code,
        meeting_date=meeting_date,
    )


def _valid_request(request: MeetingRequest) -> bool:
    if not isinstance(request, MeetingRequest):
        return False
    if isinstance(request.session_year, bool) or not isinstance(request.session_year, int):
        return False
    if not isinstance(request.meeting_date, date):
        return False
    if not re.fullmatch(r"[A-Z0-9]{1,12}", request.committee_code):
        return False
    return request.canonical_url == (
        f"https://{_SENATE_HOST}/Committees/Show/{request.committee_code}/{request.session_year}"
    )


def _meeting_document_url(index_url: str, href: str, committee_code: str) -> tuple[str, str] | None:
    if not _safe_href(href):
        return None
    candidate = urljoin(index_url, href)
    raw = urlsplit(candidate)
    if raw.query or raw.fragment:
        return None
    try:
        canonical = canonicalize_url(candidate)
    except ScoutPolicyError:
        return None
    parts = urlsplit(canonical)
    if parts.hostname != _SENATE_HOST or parts.query:
        return None
    match = _DOCUMENT_PATH_RE.fullmatch(parts.path)
    if match is None or match.group("code").upper() != committee_code:
        return None
    kind = match.group("kind").casefold()
    return canonical, "committee expanded agenda" if kind == "expandedagenda" else "committee meeting notice"


def discover_meeting_document(
    request: MeetingRequest,
    index_body: bytes,
    *,
    max_html_bytes: int = 256 * 1024,
) -> MeetingDocument | None:
    """Select one exact-date agenda/notice from the pinned committee index."""
    if not _valid_request(request):
        return None
    parser = _MeetingIndexParser()
    if not _parse_html(parser, index_body, max_html_bytes):
        return None
    if not parser.valid:
        return None
    caption = _INDEX_CAPTION_RE.fullmatch(parser.caption)
    if caption is None or int(caption.group("year")) != request.session_year:
        return None

    matching_rows: list[_IndexRow] = []
    for row in parser.rows:
        if not row.cells:
            continue
        row_date = _normalized(" ".join(row.cells[0]))
        match = _ROW_DATE_RE.fullmatch(row_date)
        if match is None:
            continue
        try:
            meeting_date = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        if meeting_date != request.meeting_date:
            continue
        matching_rows.append(row)
    # The index may list several meetings on a date.  The request has no time
    # binding, so following either row would attach an arbitrary artifact.
    if len(matching_rows) != 1:
        return None
    choices: dict[str, list[str]] = {
        "committee expanded agenda": [],
        "committee meeting notice": [],
    }
    for _label, href in matching_rows[0].anchors:
        document = _meeting_document_url(request.canonical_url, href, request.committee_code)
        if document is not None:
            canonical, artifact_type = document
            choices[artifact_type].append(canonical)
    for artifact_type in ("committee expanded agenda", "committee meeting notice"):
        urls = set(choices[artifact_type])
        if len(urls) > 1:
            # A document identifier is opaque, not a revision ordering rule.
            return None
        if urls:
            return MeetingDocument(
                canonical_url=next(iter(urls)),
                artifact_type=artifact_type,  # type: ignore[arg-type]
                session_year=request.session_year,
            )
    return None


def meeting_evidence_excerpt(text: str, bill_identifier: str, session_year: int) -> str | None:
    """Return one compact normalized proof window for a meeting-document finding."""
    if not isinstance(text, str) or isinstance(session_year, bool) or not isinstance(session_year, int):
        return None
    identifier = _EVIDENCE_IDENTIFIER_RE.fullmatch(bill_identifier) if isinstance(bill_identifier, str) else None
    if identifier is None:
        return None
    normalized = _normalized(text)
    session = re.search(rf"\b{session_year}\s+Regular\s+Session\b", normalized[:1000], re.I)
    header = re.search(
        r"\bCOMMITTEE\s+MEETING\s+(?:EXPANDED\s+AGENDA|NOTICE)\b", normalized[:1000], re.I
    )
    if session is None or header is None:
        return None
    chamber = "HB" if identifier.group("chamber").upper().replace(".", "").replace(" ", "") == "HB" else "SB"
    number = str(int(identifier.group("number")))
    bill = re.search(
        rf"(?<![A-Z0-9]){chamber[0]}\.?\s*{chamber[1]}\.?\s*{number}(?![A-Z0-9])",
        normalized,
        re.I,
    )
    if bill is None:
        return None
    # The header proves the document type near its beginning.  A bill can
    # legitimately occur on a later page, so retain a compact display window
    # around the exact bill token rather than treating distance as a failure.
    start = min(max(0, bill.start() - 200), max(0, len(normalized) - 500))
    return normalized[start : min(len(normalized), start + 500)]
