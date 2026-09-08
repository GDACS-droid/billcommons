"""Parse one bounded Florida Senate bill-detail history page without mutation.

Florida's Senate-hosted bill pages expose a human-readable Bill History table,
but no durable action identifier.  This parser therefore preserves only the
fact fields and the row/bullet positions from *this observation*.  Positions
are evidence-location aids, never occurrence identities or a promise of a
durable source ordering.

The module deliberately has no HTTP, database, queue, or corpus dependencies.
An observer must retain the exact page bytes before invoking this parser.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlsplit


ADAPTER_VERSION = "fl-senate-bill-history/1"
FL_SENATE_HOST = "www.flsenate.gov"
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_SOURCE_URL_CHARS = 1024
MAX_DOM_NODES = 20_000
MAX_DOM_DEPTH = 128
MAX_HISTORY_ROWS = 500
MAX_ACTIONS = 5_000
MAX_ACTION_TEXT_CHARS = 16 * 1024

_DETAIL_PATH = re.compile(r"^/Session/Bill/(20\d{2})/([1-9]\d*)$")
_PAGE_TITLE = re.compile(r"^(House|Senate) Bill ([1-9]\d*) \((20\d{2})\) - The Florida Senate$")
_HEADING = re.compile(r"^((?:CS/)*(?:HB|SB)) ([1-9]\d*): (.+)$")
_LAST_ACTION = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{4})\s+(?:(House|Senate)\s*-\s*)?(.+)$"
)
_SOURCE_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_HEADER = ("Date", "Chamber", "Action")
_VOID_ELEMENTS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class OfficialFloridaSenateActionsError(RuntimeError):
    """A page-shape or scope failure that must stop parsing this observation."""


@dataclass(frozen=True)
class FloridaSenateAction:
    """One bullet fact and its observation-local position in the source table."""

    action_date: date
    chamber: str | None
    description: str
    source_row_position: int
    source_bullet_position: int

    @property
    def date_precision(self) -> str:
        return "day"

    def as_evidence(self) -> dict[str, object]:
        """Return portable fact evidence without manufacturing an occurrence ID."""

        return {
            "date": self.action_date.isoformat(),
            "date_precision": self.date_precision,
            "chamber": self.chamber,
            "description": self.description,
            "source_row_position": self.source_row_position,
            "source_bullet_position": self.source_bullet_position,
        }


@dataclass(frozen=True)
class ParsedFloridaSenateBillHistory:
    """Immutable evidence parsed from one exact Florida Senate detail page."""

    source_url: str
    source_sha256: str
    session_year: str
    bill_number: str
    bill_identifier: str
    bill_title: str
    table_row_count: int
    actions: tuple[FloridaSenateAction, ...]


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[object]


class _BoundedTreeBuilder(HTMLParser):
    """A small HTML tree sufficient for the source's scoped table contract."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, [])
        self._stack = [self.root]
        self.node_count = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        self.node_count += 1
        if self.node_count > MAX_DOM_NODES:
            raise OfficialFloridaSenateActionsError("Florida Senate page exceeds DOM node cap")
        node = _Node(normalized_tag, {key.casefold(): value or "" for key, value in attrs}, [])
        self._stack[-1].children.append(node)
        if normalized_tag not in _VOID_ELEMENTS:
            # `_text` and `_descendants` deliberately use simple recursive
            # walks.  Bound the source tree while it is still being built so
            # adversarial nesting cannot turn extraction into a RecursionError.
            if len(self._stack) - 1 >= MAX_DOM_DEPTH:
                raise OfficialFloridaSenateActionsError("Florida Senate page exceeds DOM depth cap")
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        # The retained government page uses browser-tolerated optional-element
        # markup.  Close the nearest matching open element just as an HTML
        # consumer does; the extracted title, heading, and table contracts
        # below remain strict.  Treating unrelated document-wide tag repair as
        # a legislative fact contract would reject the observed source itself.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)

    def close(self) -> None:
        super().close()


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _text(node: _Node) -> str:
    pieces: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            pieces.append(item)
        elif isinstance(item, _Node):
            for child in item.children:
                visit(child)

    visit(node)
    return _normalized_text(" ".join(pieces))


def _descendants(node: _Node, tag: str | None = None) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            if tag is None or child.tag == tag:
                yield child
            yield from _descendants(child, tag)


def _direct_children(node: _Node, tag: str) -> list[_Node]:
    return [child for child in node.children if isinstance(child, _Node) and child.tag == tag]


def _line_parts(children: Iterable[object]) -> Iterable[str | None]:
    """Preserve explicit source line breaks through inline formatting."""

    for child in children:
        if isinstance(child, str):
            yield child
        elif isinstance(child, _Node):
            if child.tag == "br":
                yield None
            else:
                yield from _line_parts(child.children)


def _only(values: Iterable[_Node], what: str) -> _Node:
    found = list(values)
    if len(found) != 1:
        raise OfficialFloridaSenateActionsError(f"Florida Senate page requires exactly one {what}; found {len(found)}")
    return found[0]


def _canonical_source_url(source_url: str) -> tuple[str, str, str]:
    if not isinstance(source_url, str):
        raise OfficialFloridaSenateActionsError("Florida Senate source URL must be a string")
    if not 1 <= len(source_url) <= MAX_SOURCE_URL_CHARS:
        raise OfficialFloridaSenateActionsError("Florida Senate source URL violates length cap")
    try:
        parsed = urlsplit(source_url)
        match = _DETAIL_PATH.fullmatch(parsed.path)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise OfficialFloridaSenateActionsError("Florida Senate source URL could not be parsed") from exc
    if (
        parsed.scheme != "https"
        or hostname != FL_SENATE_HOST
        or parsed.netloc != FL_SENATE_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise OfficialFloridaSenateActionsError(
            "Florida Senate source URL must be an exact https://www.flsenate.gov/Session/Bill/<year>/<number> detail URL"
        )
    session_year, bill_number = match.groups()
    canonical_url = f"https://{FL_SENATE_HOST}/Session/Bill/{session_year}/{bill_number}"
    # urlsplit() intentionally strips leading C0 control characters and
    # whitespace.  Preserve the source-observation contract by accepting only
    # the original, byte-for-byte canonical string.
    if source_url != canonical_url:
        raise OfficialFloridaSenateActionsError(
            "Florida Senate source URL must equal its exact canonical detail URL"
        )
    return canonical_url, session_year, bill_number


def _parse_source_date(value: str, field: str) -> date:
    match = _SOURCE_DATE.fullmatch(value)
    if match is None:
        raise OfficialFloridaSenateActionsError(f"Florida Senate {field} has an invalid exact m/d/yyyy date")
    try:
        month, day, year = (int(piece) for piece in match.groups())
        return date(year, month, day)
    except ValueError as exc:
        raise OfficialFloridaSenateActionsError(f"Florida Senate {field} has an invalid exact m/d/yyyy date") from exc


def _page_scope(root: _Node, session_year: str, bill_number: str) -> tuple[str, str]:
    title = _normalized_text(_text(_only(_descendants(root, "title"), "page title")))
    title_match = _PAGE_TITLE.fullmatch(title)
    if title_match is None:
        raise OfficialFloridaSenateActionsError("Florida Senate page title does not match the bill-detail contract")
    title_chamber, title_number, title_year = title_match.groups()
    if title_number != bill_number or title_year != session_year:
        raise OfficialFloridaSenateActionsError("Florida Senate page title disagrees with source URL scope")

    heading = _normalized_text(_text(_only(_descendants(root, "h2"), "bill heading")))
    heading_match = _HEADING.fullmatch(heading)
    if heading_match is None:
        raise OfficialFloridaSenateActionsError("Florida Senate bill heading does not match the bill-detail contract")
    prefix, heading_number, bill_title = heading_match.groups()
    expected_prefix = "HB" if title_chamber == "House" else "SB"
    if prefix.rsplit("/", 1)[-1] != expected_prefix or heading_number != bill_number:
        raise OfficialFloridaSenateActionsError("Florida Senate bill heading disagrees with page title or source URL scope")
    normalized_title = _normalized_text(bill_title)
    if not normalized_title:
        raise OfficialFloridaSenateActionsError("Florida Senate bill heading has an empty title")
    return f"{prefix} {heading_number}", normalized_title


def _last_action(root: _Node) -> tuple[date, str | None, str]:
    labels = [node for node in _descendants(root, "span") if _normalized_text(_text(node)) == "Last Action:"]
    label = _only(labels, "Last Action label")
    parents = [node for node in _descendants(root) if label in node.children]
    parent = _only(parents, "Last Action parent")
    label_index = parent.children.index(label)
    pieces: list[str] = []
    ended = False
    for part in _line_parts(parent.children[label_index + 1:]):
        if part is None:
            ended = True
            break
        pieces.append(part)
    if not ended:
        raise OfficialFloridaSenateActionsError("Florida Senate Last Action field lacks a terminating line break")
    match = _LAST_ACTION.fullmatch(_normalized_text(" ".join(pieces)))
    if match is None:
        raise OfficialFloridaSenateActionsError("Florida Senate Last Action field has an invalid shape")
    raw_date, chamber, description = match.groups()
    return _parse_source_date(raw_date, "Last Action"), chamber, _normalized_text(description)


def _action_bullets(cell: _Node, row_position: int) -> list[str]:
    # The observed table separates facts with <br>, each beginning with a
    # bullet. A bullet within a fact's text is content, not another action.
    lines: list[str] = []
    pieces: list[str] = []
    for part in _line_parts(cell.children):
        if part is None:
            lines.append(_normalized_text(" ".join(pieces)))
            pieces = []
        else:
            pieces.append(part)
    lines.append(_normalized_text(" ".join(pieces)))
    values: list[str] = []
    for line in lines:
        if not line:
            continue
        if not line.startswith("•"):
            raise OfficialFloridaSenateActionsError(f"Florida Senate history row {row_position} action cell lacks bullet facts")
        value = _normalized_text(line[1:])
        if not value:
            raise OfficialFloridaSenateActionsError(f"Florida Senate history row {row_position} has malformed bullet facts")
        if len(value) > MAX_ACTION_TEXT_CHARS:
            raise OfficialFloridaSenateActionsError("Florida Senate history action exceeds text cap")
        values.append(value)
    if not values:
        raise OfficialFloridaSenateActionsError(f"Florida Senate history row {row_position} action cell lacks bullet facts")
    return values


def _history_actions(root: _Node) -> tuple[int, tuple[FloridaSenateAction, ...]]:
    history_container = _only((node for node in _descendants(root) if node.attrs.get("id") == "tabBodyBillHistory"), "Bill History container")
    table = _only(_descendants(history_container, "table"), "Bill History table")
    header = _only(_descendants(table, "thead"), "Bill History table header")
    header_row = _only(_direct_children(header, "tr"), "Bill History header row")
    if tuple(_normalized_text(_text(cell)) for cell in _direct_children(header_row, "th")) != _DATE_HEADER:
        raise OfficialFloridaSenateActionsError("Florida Senate Bill History headers must be Date, Chamber, Action")
    body = _only(_direct_children(table, "tbody"), "Bill History table body")
    rows = _direct_children(body, "tr")
    if not rows:
        raise OfficialFloridaSenateActionsError("Florida Senate Bill History table has no rows")
    if len(rows) > MAX_HISTORY_ROWS:
        raise OfficialFloridaSenateActionsError("Florida Senate Bill History table exceeds row cap")

    actions: list[FloridaSenateAction] = []
    for row_position, row in enumerate(rows, start=1):
        cells = _direct_children(row, "td")
        if len(cells) != 3:
            raise OfficialFloridaSenateActionsError(f"Florida Senate history row {row_position} must have exactly three cells")
        action_date = _parse_source_date(_normalized_text(_text(cells[0])), f"history row {row_position}")
        chamber_text = _normalized_text(_text(cells[1]))
        if chamber_text not in {"", "House", "Senate"}:
            raise OfficialFloridaSenateActionsError(f"Florida Senate history row {row_position} has an invalid chamber")
        chamber = chamber_text or None
        for bullet_position, description in enumerate(_action_bullets(cells[2], row_position), start=1):
            if len(actions) >= MAX_ACTIONS:
                raise OfficialFloridaSenateActionsError("Florida Senate Bill History table exceeds action cap")
            actions.append(FloridaSenateAction(action_date, chamber, description, row_position, bullet_position))
    return len(rows), tuple(actions)


def parse_florida_senate_bill_history(raw_html: bytes, *, source_url: str) -> ParsedFloridaSenateBillHistory:
    """Parse one exact Florida Senate bill page and validate its action summary.

    The accepted URL, page title, ``h2`` identifier/title, one Bill History
    table, three columns, dates, chambers, and summary Last Action all have to
    agree.  Any source-shape change raises ``OfficialFloridaSenateActionsError``
    before a caller can treat the observation as partial action data.
    """

    canonical_url, session_year, bill_number = _canonical_source_url(source_url)
    if not isinstance(raw_html, bytes):
        raise TypeError("Florida Senate page bytes must be bytes")
    if not 1 <= len(raw_html) <= MAX_HTML_BYTES:
        raise OfficialFloridaSenateActionsError("Florida Senate page violates byte bounds")
    try:
        document = raw_html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialFloridaSenateActionsError("Florida Senate page is not valid UTF-8") from exc

    parser = _BoundedTreeBuilder()
    try:
        parser.feed(document)
        parser.close()
    except OfficialFloridaSenateActionsError:
        raise
    except Exception as exc:
        raise OfficialFloridaSenateActionsError("Florida Senate page could not be parsed as HTML") from exc
    bill_identifier, bill_title = _page_scope(parser.root, session_year, bill_number)
    last_action = _last_action(parser.root)
    row_count, actions = _history_actions(parser.root)
    if not actions:
        raise OfficialFloridaSenateActionsError("Florida Senate Bill History table has no action bullets")
    final_action = actions[-1]
    if (final_action.action_date, final_action.chamber, final_action.description) != last_action:
        raise OfficialFloridaSenateActionsError("Florida Senate Last Action field disagrees with final Bill History action")
    if final_action.action_date != max(action.action_date for action in actions):
        raise OfficialFloridaSenateActionsError("Florida Senate Last Action day does not equal the latest Bill History action day")
    return ParsedFloridaSenateBillHistory(
        source_url=canonical_url,
        source_sha256=hashlib.sha256(raw_html).hexdigest(),
        session_year=session_year,
        bill_number=bill_number,
        bill_identifier=bill_identifier,
        bill_title=bill_title,
        table_row_count=row_count,
        actions=actions,
    )
