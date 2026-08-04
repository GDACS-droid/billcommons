"""billcommons_shared.topics is the single source of truth for the curated
topic registry -- billcommons_api.routers.topics and billcommons_mcp's
list_topics tool both import TOPICS/membership_clause from here rather than
each carrying their own copy (see that module's docstring for the drift bug
this replaces). These tests don't touch the DB (this package's suite doesn't
have one), so they pin the registry's own integrity and that the query
builder produces a real, compilable SQLAlchemy expression -- not whether a
given bill matches, which is covered where a DB is available (apps/api's
test_stats_and_topics.py, apps/mcp's test_list_topics.py).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from billcommons_schema.models import Bill
from billcommons_shared.topics import TOPICS, Topic, membership_clause


def test_every_topic_has_a_slug_matching_its_dict_key():
    for slug, topic in TOPICS.items():
        assert topic.slug == slug


def test_every_topic_has_at_least_one_title_pattern():
    """membership_clause ORs title_patterns with subject_patterns; a topic
    with neither would build an empty or_() that matches nothing (or, worse,
    everything, depending on how SQLAlchemy renders an empty BooleanClauseList)."""
    for topic in TOPICS.values():
        assert topic.title_patterns, f"{topic.slug} has no title_patterns"
        for pattern in topic.title_patterns:
            assert pattern.startswith("%") and pattern.endswith("%"), (
                f"{topic.slug}: {pattern!r} is not a LIKE wildcard pattern"
            )


def test_slugs_are_url_safe():
    for slug in TOPICS:
        assert slug == slug.lower()
        assert " " not in slug


def test_membership_clause_builds_a_real_sql_expression():
    topic = next(iter(TOPICS.values()))
    clause = membership_clause(topic)
    assert isinstance(clause, ColumnElement)
    # Compiles to real SQL without a live DB connection -- proves the clause
    # is well-formed (correct column refs, valid EXISTS subquery), which is
    # the failure mode that would otherwise only surface at request time.
    compiled = str(select(Bill.id).where(clause))
    assert "bills" in compiled.lower()


def test_topic_is_frozen_and_hashable():
    """Topic instances are shared module-level state (imported by two other
    packages); accidental mutation from one caller must not be possible."""
    topic = Topic(slug="x", name="X", description="d", title_patterns=("%x%",))
    hash(topic)  # frozen dataclasses are hashable; mutable ones are not
