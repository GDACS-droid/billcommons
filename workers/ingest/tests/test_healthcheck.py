"""Tests for the crawl liveness check.

Business intent: this check exists because the crawl went dead twice while
every conventional signal said healthy -- service Online, logs scrolling,
queue depth comfortable. Its whole value is distinguishing "busy" from
"producing", and refusing to call an idle-but-finished crawl a failure. If
these tests can't fail when that distinction is broken, the check is
decoration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billcommons_ingest.healthcheck import check_crawl_health


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDb:
    """Answers the check's queries by matching on a distinctive fragment of
    each, so the test states the WORLD (what the DB contains) rather than
    mirroring the check's own SQL."""

    def __init__(self, *, last_text_at, texted_last_hour, claimable, queued, dead, backlog):
        self.values = {
            "max(updated_at)": last_text_at,
            "and updated_at > :cutoff": texted_last_hour,
            "run_after <= :now": claimable,
            "status='queued'": queued,
            "status='dead'": dead,
            "select exists": backlog,
        }

    def execute(self, stmt, params=None):
        sql = str(stmt)
        # Most specific fragments first: the queued/dead counts share text
        # with the claimable query.
        for fragment in (
            "select exists",
            "max(updated_at)",
            "and updated_at > :cutoff",
            "run_after <= :now",
            "status='dead'",
            "status='queued'",
        ):
            if fragment in sql:
                return _FakeResult(self.values[fragment])
        raise AssertionError(f"unexpected query: {sql}")


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _db(**kwargs):
    defaults = dict(
        last_text_at=NOW - timedelta(minutes=1),
        texted_last_hour=2000,
        claimable=500,
        queued=500,
        dead=10,
        backlog=True,
    )
    defaults.update(kwargs)
    return _FakeDb(**defaults)


def test_producing_crawl_is_healthy():
    health = check_crawl_health(_db(), now=NOW)
    assert health.healthy is True
    assert "producing" in health.reason


def test_claimable_work_but_nothing_extracted_is_stalled():
    """The exact 2026-07-25 shape: jobs available and being claimed, zero
    output, service reporting Online. This is the case the check exists for."""
    health = check_crawl_health(
        _db(last_text_at=NOW - timedelta(minutes=90), texted_last_hour=0, claimable=1215),
        now=NOW,
    )
    assert health.healthy is False
    assert "1,215" in health.reason
    assert health.minutes_since_text == 90.0


def test_empty_queue_is_idle_not_stalled():
    """A crawl with nothing left to do has also produced nothing recently.
    Calling that STALLED would make the check fire permanently once the
    corpus is complete, which is how alerts get muted."""
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(days=3),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            backlog=False,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert "idle, not stalled" in health.reason


def test_empty_queue_with_backlog_remaining_is_stalled():
    """The 2026-07-26 outage. The top-up query started failing, so no new
    fetch_text jobs were created; the queue drained to zero and the crawl
    stopped. Judging on the queue alone, this looks identical to a finished
    corpus -- and the first version of this check called it healthy and sent
    a RECOVERED alert while ~613k documents sat unfetched. The corpus, not
    the queue, decides whether there is work left to do."""
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=131),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            backlog=True,
        ),
        now=NOW,
    )
    assert health.healthy is False
    assert "top-up is not producing" in health.reason


def test_briefly_empty_queue_between_top_ups_is_not_stalled():
    """The queue legitimately hits zero for a moment each time a batch is
    consumed before the next top-up runs. Firing on that would alert several
    times an hour, and an alert that noisy gets muted."""
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=2),
            texted_last_hour=3000,
            claimable=0,
            queued=0,
            backlog=True,
        ),
        now=NOW,
    )
    assert health.healthy is True


def test_backed_off_queue_does_not_count_as_claimable():
    """`queued` can be large while nothing is actually eligible -- that gap is
    what let the deadlock hide. The check reports claimable separately and
    judges on it, so a queue full of backed-off jobs reads as idle rather than
    as healthy backlog."""
    health = check_crawl_health(
        _db(last_text_at=NOW - timedelta(hours=5), texted_last_hour=0, claimable=0, queued=4000),
        now=NOW,
    )
    assert health.healthy is True
    assert health.queued_total == 4000
    assert health.claimable_now == 0


def test_never_extracted_anything_is_stalled():
    health = check_crawl_health(
        _db(last_text_at=None, texted_last_hour=0, claimable=100), now=NOW
    )
    assert health.healthy is False
    assert "EVER" in health.reason
    assert health.minutes_since_text is None


def test_slow_but_moving_crawl_is_not_flagged():
    """Extraction rate legitimately swings several-fold with document mix
    (small HTML vs large scanned PDFs). A check that fires on ordinary
    slowness gets muted, and then the next real stall is invisible again."""
    health = check_crawl_health(
        _db(last_text_at=NOW - timedelta(minutes=29), texted_last_hour=3), now=NOW
    )
    assert health.healthy is True


def test_stall_threshold_is_configurable():
    db_args = dict(last_text_at=NOW - timedelta(minutes=40), texted_last_hour=0, claimable=100)
    assert check_crawl_health(_db(**db_args), now=NOW, stall_minutes=30).healthy is False
    assert check_crawl_health(_db(**db_args), now=NOW, stall_minutes=60).healthy is True


def test_naive_timestamp_from_the_database_is_handled():
    """A tz-naive datetime must not raise -- an exception here would surface
    as CHECK-FAILED and be indistinguishable from a real outage."""
    health = check_crawl_health(
        _db(last_text_at=datetime(2026, 7, 25, 11, 59)), now=NOW
    )
    assert health.healthy is True
    assert health.minutes_since_text == 1.0
