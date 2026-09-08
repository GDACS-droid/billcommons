import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from billcommons_ingest import official_discovery as discovery
from billcommons_ingest.official_observer import observe_due_target
from billcommons_ingest.official_worker import seed_discovery_targets
from billcommons_schema.models import (
    Jurisdiction, OfficialRawBlob, OfficialSourceObservation, OfficialSourceTarget,
)


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def target(db):
    row = Jurisdiction(abbreviation="NC", name="North Carolina", classification="state")
    db.add(row)
    db.flush()
    source = OfficialSourceTarget(jurisdiction_id=row.id, adapter_name=discovery.ADAPTER_NAME,
        source_url=discovery.official_source_inventory()["NC"], enabled=True,
        next_check_at=NOW, cadence_seconds=86400,
        scope={"jurisdiction": "NC", "inventory_version": discovery.INVENTORY_VERSION,
               "coverage": "bounded_link_discovery"})
    db.add(source)
    db.flush()
    return source


def test_failed_policy_is_retained_without_claiming_page_freshness(db_session, monkeypatch):
    source = target(db_session)
    policy = b"User-agent: *\nDisallow: /\n"
    captured = discovery.OfficialDiscoveryCapture(source_url=source.source_url, retrieved_at=NOW,
        robots_url="https://www.ncleg.gov/robots.txt", robots_status=200,
        robots_bytes=policy, error_class="robots_disallowed")
    monkeypatch.setattr(discovery, "capture_official_landing_page", lambda *args: captured)
    result = observe_due_target(db_session, now=NOW)
    db_session.flush()
    assert result.status == "failed"
    observation = db_session.scalar(select(OfficialSourceObservation))
    assert observation.adapter_name == discovery.ADAPTER_NAME
    assert observation.raw_sha256 is None
    assert observation.http_status is None
    policy_sha = observation.scope["robots"]["raw_sha256"]
    assert policy_sha == hashlib.sha256(policy).hexdigest()
    assert db_session.get(OfficialRawBlob, policy_sha).data == policy
    assert observation.scope["legislative_freshness"] == "not_established"
    assert source.consecutive_failures == 1
    assert source.next_check_at > NOW


def test_changed_page_retains_replayable_link_diff_without_inferring_removal(db_session, monkeypatch):
    source = target(db_session)
    def capture(path, when):
        raw = ('<a href="/' + path + '">Bill</a>').encode()
        links, _ = discovery.discover_material_links(raw, source_url=source.source_url)
        return discovery.OfficialDiscoveryCapture(source_url=source.source_url, retrieved_at=when,
            http_status=200, raw_bytes=raw, content_type="text/html",
            robots_url="https://www.ncleg.gov/robots.txt", robots_status=404,
            robots_bytes=b"not found", links=links)
    first = capture("old.pdf", NOW)
    monkeypatch.setattr(discovery, "capture_official_landing_page", lambda *args: first)
    assert observe_due_target(db_session, now=NOW).status == "succeeded"
    db_session.flush()
    previous = db_session.scalar(select(OfficialSourceObservation))
    second = capture("new.pdf", NOW + timedelta(days=1))
    monkeypatch.setattr(discovery, "capture_official_landing_page", lambda *args: second)
    result = observe_due_target(db_session, now=NOW + timedelta(days=1))
    assert result.status == "succeeded"
    assert result.record_count == 1
    db_session.flush()
    current = db_session.scalar(select(OfficialSourceObservation).where(OfficialSourceObservation.id != previous.id))
    assert current.scope["content_changed"] is True
    assert current.scope["previous_observation_id"] == str(previous.id)
    diff = json.loads(db_session.get(OfficialRawBlob, current.scope["discovery_diff_sha256"]).data)
    assert diff["previous_source_sha256"] == previous.raw_sha256
    assert diff["newly_observed_links"][0]["url"].endswith("/new.pdf")
    assert diff["not_observed_this_time"][0]["url"].endswith("/old.pdf")
    assert diff["absence_is_not_removal"] is True


def test_all_state_registration_is_disabled_until_explicit_enable(db_session):
    inventory = discovery.official_source_inventory()
    db_session.add_all(Jurisdiction(abbreviation=code, name=code, classification="state") for code in inventory)
    db_session.flush()
    assert seed_discovery_targets(db_session) == 51
    rows = db_session.scalars(select(OfficialSourceTarget)).all()
    assert len(rows) == 51
    assert not any(row.enabled for row in rows)
    assert seed_discovery_targets(db_session, enable=True) == 51
    assert len(db_session.scalars(select(OfficialSourceTarget)).all()) == 51
    assert all(row.enabled for row in rows)
