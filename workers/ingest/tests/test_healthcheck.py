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
from billcommons_ingest.fulltext import AWAITING_UPSTREAM_STATUSES, TERMINAL_STATUSES


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDb:
    """Answers the check's queries by matching on a distinctive fragment of
    each, so the test states the WORLD (what the DB contains) rather than
    mirroring the check's own SQL."""

    def __init__(
        self,
        *,
        last_text_at,
        texted_last_hour,
        claimable,
        queued,
        dead,
        backlog,
        awaiting=0,
        uncovered=None,
        running=0,
        stale_running=0,
        actionable_queued=None,
    ):
        if uncovered is None:
            uncovered = backlog and queued == 0 and running == 0
        if actionable_queued is None:
            actionable_queued = queued
        self.executed = []
        self.values = {
            "max(updated_at)": last_text_at,
            "and updated_at > :cutoff": texted_last_hour,
            "as awaiting_upstream": awaiting,
            "as actionable_queued": actionable_queued,
            "as running_total": running,
            "as stale_running": stale_running,
            "j.status in ('queued', 'running')": uncovered,
            "run_after <= :now": claimable,
            "status='queued'": queued,
            "status='dead'": dead,
            "select exists": backlog,
        }

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((stmt, params))
        # Most specific fragments first: the queued/dead counts share text
        # with the claimable query.
        for fragment in (
            "max(updated_at)",
            "and updated_at > :cutoff",
            # Before "run_after"/"status='queued'": the awaiting-upstream
            # count also filters on queued status and would otherwise be
            # answered with the wrong number.
            "as awaiting_upstream",
            "as actionable_queued",
            "as running_total",
            "as stale_running",
            "j.status in ('queued', 'running')",
            "select exists",
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
        awaiting=0,
        uncovered=None,
        running=0,
        stale_running=0,
        actionable_queued=None,
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
    assert health.status == "stalled"
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


def test_queue_of_upstream_blocked_jobs_is_not_a_stall():
    """2026-08-30, ~22 hours of Telegram alerts every 10 minutes.

    The whole fetch_text queue was 61 Massachusetts dockets that MA has not
    assigned bill numbers to yet. They retry on a 60/120/240/480s backoff, so
    whether any were past `run_after` at the moment the 10-minute monitor
    ticked was effectively a coin flip -- the verdict alternated
    healthy/stalled every single run and the monitor alerted on each flip.
    These jobs cannot produce text no matter how healthy the crawl is, so
    they must not count as "work is available".
    """
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=1337),
            texted_last_hour=0,
            claimable=0,
            awaiting=61,
            queued=61,
            backlog=False,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.awaiting_upstream == 61
    assert "upstream" in health.reason
    assert "top-up" not in health.reason
    assert health.status == "waiting_upstream"


def test_upstream_waiters_do_not_mask_a_real_stall():
    """The 2026-08-30 fix must not buy quiet by going blind: real claimable
    work that produces nothing is still the 2026-07-25 shape, whatever else
    happens to be sitting in the queue behind it."""
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=90),
            texted_last_hour=0,
            claimable=1215,
            awaiting=61,
            queued=1276,
            backlog=True,
        ),
        now=NOW,
    )
    assert health.healthy is False
    assert "1,215" in health.reason


def test_upstream_only_documents_with_no_queue_are_waiting_not_stalled():
    """The documented flapping interval has no queue row between retries."""
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=12),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            awaiting=176,
            backlog=False,
            uncovered=False,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.status == "waiting_upstream"


def test_new_text_is_producing_even_after_queue_drains_to_upstream_only():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=1),
            texted_last_hour=1,
            claimable=0,
            queued=0,
            awaiting=176,
            backlog=False,
            uncovered=False,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.status == "producing"


def test_claimable_work_with_old_text_is_stalled_even_if_hour_count_is_nonzero():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=45),
            texted_last_hour=1,
            claimable=1,
            queued=1,
            backlog=True,
            uncovered=False,
        ),
        now=NOW,
    )
    assert health.healthy is False
    assert health.status == "stalled"


def test_claimable_work_with_old_text_is_not_excused_by_a_fresh_running_job():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=2),
            texted_last_hour=0,
            claimable=1,
            queued=1,
            backlog=True,
            uncovered=False,
            running=1,
        ),
        now=NOW,
    )
    assert health.healthy is False
    assert health.status == "stalled"


def test_uncovered_document_is_not_starved_with_actionable_queued_coverage():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=2),
            texted_last_hour=0,
            claimable=0,
            queued=1,
            actionable_queued=1,
            backlog=True,
            uncovered=True,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.status == "idle_or_backoff"


def test_uncovered_document_is_not_starved_with_actionable_running_coverage():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=2),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            actionable_queued=0,
            backlog=True,
            uncovered=True,
            running=1,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.status == "running"


def test_future_database_timestamp_is_clamped_to_zero_minutes():
    health = check_crawl_health(
        _db(last_text_at=NOW + timedelta(milliseconds=1), texted_last_hour=1), now=NOW
    )
    assert health.healthy is True
    assert health.minutes_since_text == 0.0


def test_longer_threshold_does_not_label_zero_output_as_producing():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(minutes=45),
            texted_last_hour=0,
            claimable=1,
            queued=1,
            backlog=True,
            uncovered=False,
        ),
        now=NOW,
        stall_minutes=60,
    )
    assert health.healthy is True
    assert health.status == "idle_or_backoff"


def test_fresh_running_job_is_not_mistaken_for_an_empty_queue():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=2),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            backlog=True,
            uncovered=False,
            running=1,
        ),
        now=NOW,
    )
    assert health.healthy is True
    assert health.status == "running"


def test_stale_running_job_is_a_distinct_stall():
    health = check_crawl_health(
        _db(
            last_text_at=NOW - timedelta(hours=2),
            texted_last_hour=0,
            claimable=0,
            queued=0,
            backlog=True,
            uncovered=False,
            running=1,
            stale_running=1,
        ),
        now=NOW,
    )
    assert health.healthy is False
    assert health.status == "stalled"
    assert "running longer" in health.reason


def test_actionable_query_matches_exact_or_space_decorated_status_tokens():
    db = _db(backlog=False)
    check_crawl_health(db, now=NOW)
    statements = [str(stmt) for stmt, _ in db.executed]
    params = [params or {} for _, params in db.executed]
    terminal_match = "split_part(coalesce(d.license_note, ''), ' ', 1) = any(:terminal)"
    awaiting_match = "split_part(coalesce(d.license_note, ''), ' ', 1) = any(:awaiting)"
    assert any(terminal_match in statement for statement in statements)
    assert any(awaiting_match in statement for statement in statements)
    assert not any(" like any(" in statement for statement in statements)
    merged = {key: value for query_params in params for key, value in query_params.items()}
    assert set(merged["terminal"]) == {f"fulltext_status={status}" for status in TERMINAL_STATUSES}
    assert set(merged["awaiting"]) == {f"fulltext_status={status}" for status in AWAITING_UPSTREAM_STATUSES}
