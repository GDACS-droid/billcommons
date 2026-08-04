"""list_topics is the MCP discoverability fix: the consumer concluded "no
topic tags" because nothing on the tool surface pointed at the curated
topic registry that already existed behind the REST API's /topics.

Two things this file pins down:
  * the tool actually queries billcommons_shared.topics.TOPICS (the single
    shared registry -- see that module's docstring for why a second,
    hand-duplicated copy was the wrong fix), not a second local copy.
  * it goes through the same _run_tool/telemetry chokepoint as every other
    tool, so it gets self-probe tagging for free rather than needing its
    own carve-out (see test_self_probe_tagging.py).
"""
from __future__ import annotations

import inspect

import billcommons_mcp.tools as tools
from billcommons_shared.topics import TOPICS


def test_list_topics_returns_every_curated_topic():
    result = tools.list_topics()
    assert "error" not in result, result
    slugs = {t["slug"] for t in result["topics"]}
    assert slugs == set(TOPICS), "list_topics drifted from billcommons_shared.topics.TOPICS"
    assert result["result_count"] == len(TOPICS)


def test_each_topic_carries_name_description_count_and_fetch_hint():
    result = tools.list_topics()
    for topic in result["topics"]:
        assert topic["name"], topic
        assert topic["description"], topic
        assert isinstance(topic["bill_count"], int)
        assert topic["bill_count"] >= 0
        # Actionable, not a bare pointer to a webpage a machine can't browse:
        # a REST URL a client could actually GET.
        assert f"/api/v1/topics/{topic['slug']}" in topic["how_to_fetch_bills"]


def test_meta_envelope_present():
    result = tools.list_topics()
    assert "retrieved_at" in result["meta"]


def test_list_topics_shares_the_registry_not_a_local_copy():
    """Regression guard for the actual bug being fixed: importing TOPICS from
    billcommons_shared rather than re-declaring it (which is exactly how the
    API and the alerts worker drifted before -- see topics.py's docstring)."""
    import billcommons_mcp.tools as tools_module

    src = inspect.getsource(tools_module)
    assert "from billcommons_shared.topics import" in src
    assert "TOPICS: dict" not in src, "list_topics must not re-declare a local TOPICS registry"


def test_list_topics_goes_through_the_telemetry_chokepoint():
    """Every tool must record via _run_tool -- see tools.py's _run_tool
    docstring on why this is the one place usage is measured."""
    src = inspect.getsource(tools.list_topics)
    assert '_run_tool(body, "list_topics")' in src


def test_topic_counts_are_cached_within_the_ttl(monkeypatch):
    """list_topics is the call-first discovery tool on a public, anonymous,
    keyless, uncached surface -- a second call inside the TTL must not
    re-run the corpus-wide COUNT queries. See _cached_topic_bill_count's
    comment (referencing 3d82eee, the same class of cost on the web side)."""
    tools._topic_count_cache.clear()
    calls: list[str] = []
    real_topic_bill_count = tools.topic_bill_count

    def spy(db, topic):
        calls.append(topic.slug)
        return real_topic_bill_count(db, topic)

    monkeypatch.setattr(tools, "topic_bill_count", spy)

    tools.list_topics()
    first_pass_calls = len(calls)
    assert first_pass_calls == len(TOPICS), "first call (cold cache) must query every topic"

    tools.list_topics()
    assert len(calls) == first_pass_calls, (
        "a second call within the TTL re-queried the DB instead of using the cache"
    )
