from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from billcommons_ingest import official_discovery as discovery
from billcommons_ingest import official_fl_senate_registration as registration
from billcommons_ingest import official_worker
from billcommons_schema.models import (
    Jurisdiction,
    OfficialSourceObservation,
    OfficialSourceTarget,
)
from billcommons_shared.db import get_engine


NOW = datetime(2026, 9, 8, 15, 30, tzinfo=timezone.utc)


def _seed_reviewed_initial_inventory(db):
    inventory = discovery.official_source_inventory()
    jurisdictions = {
        code: Jurisdiction(abbreviation=code, name=code, classification="state")
        for code in inventory
    }
    db.add_all(jurisdictions.values())
    db.flush()
    assert official_worker.seed_discovery_targets(db) == 51
    assert official_worker.seed_ca_targets(db) == 7
    return jurisdictions


def _florida_target(db):
    return db.scalar(select(OfficialSourceTarget).where(
        OfficialSourceTarget.adapter_name == registration.fl_capture.ADAPTER_NAME,
        OfficialSourceTarget.source_url == registration.FLORIDA_SOURCE_URL,
    ))


def _add_exact_florida_target(db, jurisdictions):
    target = OfficialSourceTarget(
        jurisdiction_id=jurisdictions["FL"].id,
        adapter_name=registration.fl_capture.ADAPTER_NAME,
        source_url=registration.FLORIDA_SOURCE_URL,
        scope=dict(registration.FLORIDA_SCOPE),
        enabled=False,
        cadence_seconds=registration.CADENCE_SECONDS,
    )
    db.add(target)
    db.flush()
    return target


def test_registers_only_exact_disabled_fifty_ninth_target_and_is_idempotent(db_session, monkeypatch):
    jurisdictions = _seed_reviewed_initial_inventory(db_session)
    before = {target.id: (target.next_check_at, target.consecutive_failures) for target in db_session.scalars(select(OfficialSourceTarget))}
    monkeypatch.setattr(db_session, "commit", lambda: pytest.fail("registration must not commit"))

    registered = registration.register_reviewed_fl_senate_target(db_session)
    target = _florida_target(db_session)
    assert registered.initial_target_count == 58
    assert registered.target_count == 59
    assert registered.target_id == target.id
    assert registered.jurisdiction_id == jurisdictions["FL"].id
    assert dict(registered.scope) == registration.FLORIDA_SCOPE
    assert target.enabled is False
    assert target.cadence_seconds == 86_400
    assert target.scope == registration.FLORIDA_SCOPE
    assert len(db_session.scalars(select(OfficialSourceTarget)).all()) == 59
    assert {target.id: (target.next_check_at, target.consecutive_failures) for target in db_session.scalars(select(OfficialSourceTarget)) if target.id in before} == before

    target.next_check_at = NOW + timedelta(days=3)
    target.consecutive_failures = 4
    repeated = registration.register_reviewed_fl_senate_target(db_session)
    assert repeated.target_id == target.id
    assert repeated.initial_target_count == 59
    assert target.next_check_at == NOW + timedelta(days=3)
    assert target.consecutive_failures == 4


def test_registration_rolls_back_with_the_callers_savepoint(db_session):
    _seed_reviewed_initial_inventory(db_session)
    savepoint = db_session.begin_nested()
    registration.register_reviewed_fl_senate_target(db_session)
    assert len(db_session.scalars(select(OfficialSourceTarget)).all()) == 59
    savepoint.rollback()
    assert len(db_session.scalars(select(OfficialSourceTarget)).all()) == 58


def test_registration_flushes_pending_jurisdiction_edit_before_inventory_lookup():
    engine = get_engine()
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint", autoflush=False) as db:
                florida = _seed_reviewed_initial_inventory(db)["FL"]
                florida.name = "Pending Florida registration edit"
                with db.no_autoflush:
                    registration.register_reviewed_fl_senate_target(db)
                assert florida.name == "Pending Florida registration edit"
                assert connection.scalar(
                    select(Jurisdiction.name).where(Jurisdiction.id == florida.id)
                ) == florida.name
        finally:
            outer.rollback()


def test_registration_advisory_lock_rejects_a_second_transaction(db_session):
    _seed_reviewed_initial_inventory(db_session)
    registration.register_reviewed_fl_senate_target(db_session)
    competing = Session(get_engine())
    try:
        with pytest.raises(registration.FloridaSenateRegistrationError, match="advisory lock"):
            registration.register_reviewed_fl_senate_target(competing)
    finally:
        competing.rollback()
        competing.close()


def test_registration_rejects_extra_or_enabled_or_continuing_initial_inventory(db_session):
    jurisdictions = _seed_reviewed_initial_inventory(db_session)
    db_session.add(OfficialSourceTarget(
        jurisdiction_id=jurisdictions["FL"].id,
        adapter_name="unexpected_adapter",
        source_url="https://www.flsenate.gov/unreviewed",
        scope={}, enabled=False, cadence_seconds=86_400,
    ))
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="inventory"):
        registration.register_reviewed_fl_senate_target(db_session)

    db_session.delete(db_session.scalars(select(OfficialSourceTarget).where(
        OfficialSourceTarget.adapter_name == "unexpected_adapter"
    )).one())
    ca = db_session.scalars(select(OfficialSourceTarget).where(
        OfficialSourceTarget.adapter_name == "ca_official_actions"
    )).first()
    original_scope = dict(ca.scope)
    ca.scope = {**ca.scope, "continuation": {}}
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="scope"):
        registration.register_reviewed_fl_senate_target(db_session)

    ca.scope = original_scope
    ca.enabled = True
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="disabled"):
        registration.register_reviewed_fl_senate_target(db_session)


def test_registration_rejects_existing_florida_target_with_drift_or_enablement(db_session):
    jurisdictions = _seed_reviewed_initial_inventory(db_session)
    registration.register_reviewed_fl_senate_target(db_session)
    target = _florida_target(db_session)
    target.scope = {**registration.FLORIDA_SCOPE, "coverage": "unbounded"}
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="scope"):
        registration.register_reviewed_fl_senate_target(db_session)

    target.scope = dict(registration.FLORIDA_SCOPE)
    target.enabled = True
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="disabled"):
        registration.register_reviewed_fl_senate_target(db_session)

    target.enabled = False
    replacement_jurisdiction = Jurisdiction(
        abbreviation="ZZ_FL_REPLACEMENT", name="Replacement", classification="state"
    )
    db_session.add(replacement_jurisdiction)
    db_session.flush()
    target.jurisdiction_id = replacement_jurisdiction.id
    with pytest.raises(registration.FloridaSenateRegistrationError, match="jurisdiction"):
        registration.register_reviewed_fl_senate_target(db_session)
    assert target.jurisdiction_id != jurisdictions["FL"].id


def test_activation_locks_and_enables_only_pristine_florida_target(db_session, monkeypatch):
    _seed_reviewed_initial_inventory(db_session)
    registered = registration.register_reviewed_fl_senate_target(db_session)
    target = _florida_target(db_session)
    target.consecutive_failures = 3
    previous_check = target.next_check_at
    monkeypatch.setattr(db_session, "commit", lambda: pytest.fail("activation must not commit"))

    activated = registration.activate_reviewed_fl_senate_target(db_session, now=NOW)
    assert activated.target_id == registered.target_id
    assert activated.target_count == 59
    assert dict(activated.scope) == registration.FLORIDA_SCOPE
    assert target.enabled is True
    assert target.next_check_at == NOW
    assert target.consecutive_failures == 3
    assert target.cadence_seconds == registration.CADENCE_SECONDS
    assert target.scope == registration.FLORIDA_SCOPE
    assert previous_check != target.next_check_at

    with pytest.raises(registration.FloridaSenateActivationError, match="already enabled"):
        registration.activate_reviewed_fl_senate_target(db_session, now=NOW)


def test_activation_flushes_pending_jurisdiction_edit_before_inventory_lookup():
    engine = get_engine()
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint", autoflush=False) as db:
                florida = _seed_reviewed_initial_inventory(db)["FL"]
                registration.register_reviewed_fl_senate_target(db)
                florida.name = "Pending Florida activation edit"
                with db.no_autoflush:
                    registration.activate_reviewed_fl_senate_target(db, now=NOW)
                assert florida.name == "Pending Florida activation edit"
                assert connection.scalar(
                    select(Jurisdiction.name).where(Jurisdiction.id == florida.id)
                ) == florida.name
        finally:
            outer.rollback()


def test_activation_rolls_back_and_locks_only_the_florida_target(db_session):
    _seed_reviewed_initial_inventory(db_session)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        registration.register_reviewed_fl_senate_target(db_session)
        target = _florida_target(db_session)
        original_check = target.next_check_at
        savepoint = db_session.begin_nested()
        registration.activate_reviewed_fl_senate_target(db_session, now=NOW)
        assert target.enabled is True
        savepoint.rollback()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    db_session.refresh(target)
    assert target.enabled is False
    assert target.next_check_at == original_check
    locked = [statement for statement in statements if "for update" in statement]
    assert len(locked) == 1
    assert "official_source_targets" in locked[0]
    assert "where official_source_targets.id" in locked[0]
    assert all("bills" not in statement and "bill_actions" not in statement for statement in statements)


def test_second_activation_transaction_loses_the_advisory_lock():
    engine = get_engine()
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint") as setup:
                jurisdictions = _seed_reviewed_initial_inventory(setup)
                target = _add_exact_florida_target(setup, jurisdictions)
                target_id = target.id
                setup.commit()
            with Session(connection, join_transaction_mode="create_savepoint") as first:
                registration.activate_reviewed_fl_senate_target(first, now=NOW)
                with Session(engine) as second:
                    # The second call must fail on the lock before it reads the
                    # inventory, so the fixture need not become globally visible.
                    assert second.get(OfficialSourceTarget, target_id) is None
                    with pytest.raises(registration.FloridaSenateRegistrationError, match="advisory lock"):
                        registration.activate_reviewed_fl_senate_target(second, now=NOW)
                first.rollback()
            with Session(connection, join_transaction_mode="create_savepoint") as check:
                assert check.get(OfficialSourceTarget, target_id).enabled is False
        finally:
            outer.rollback()


@pytest.mark.parametrize("action", ["register", "activate"])
def test_registry_refreshes_previously_cached_target_state(db_session, action):
    _seed_reviewed_initial_inventory(db_session)
    registration.register_reviewed_fl_senate_target(db_session)
    cached = _florida_target(db_session)
    scheduled = NOW + timedelta(days=2)
    # Bypass the identity map as an independently committed write would, while
    # keeping all fixture rows inside the disposable test transaction.
    db_session.connection().execute(update(OfficialSourceTarget).where(
        OfficialSourceTarget.id == cached.id
    ).values(enabled=True, next_check_at=scheduled))
    assert cached.enabled is False
    if action == "register":
        with pytest.raises(registration.FloridaSenateRegistrationError, match="disabled"):
            registration.register_reviewed_fl_senate_target(db_session)
    else:
        with pytest.raises(registration.FloridaSenateActivationError, match="already enabled"):
            registration.activate_reviewed_fl_senate_target(db_session, now=NOW)
    db_session.refresh(cached)
    assert cached.enabled is True and cached.next_check_at == scheduled


def test_activation_revalidates_scope_after_acquiring_row_lock(db_session, monkeypatch):
    _seed_reviewed_initial_inventory(db_session)
    registered = registration.register_reviewed_fl_senate_target(db_session)
    validate = registration._validate_inventory

    def drift_after_inventory(db, **kwargs):
        result = validate(db, **kwargs)
        db.connection().execute(update(OfficialSourceTarget).where(
            OfficialSourceTarget.id == registered.target_id
        ).values(scope={**registration.FLORIDA_SCOPE, "coverage": "unreviewed"}))
        return result

    monkeypatch.setattr(registration, "_validate_inventory", drift_after_inventory)
    with pytest.raises(registration.FloridaSenateActivationError, match="changed before activation"):
        registration.activate_reviewed_fl_senate_target(db_session, now=NOW)
    target = _florida_target(db_session)
    db_session.refresh(target)
    assert target.enabled is False and target.scope["coverage"] == "unreviewed"


def test_activation_refuses_previously_observed_or_naive_time(db_session):
    _seed_reviewed_initial_inventory(db_session)
    registration.register_reviewed_fl_senate_target(db_session)
    target = _florida_target(db_session)
    db_session.add(OfficialSourceObservation(
        target_id=target.id,
        adapter_name=target.adapter_name,
        adapter_version="test/1",
        source_url=target.source_url,
        scope=target.scope,
        retrieved_at=NOW,
        status="failed",
        error_class="test",
    ))
    db_session.flush()
    with pytest.raises(registration.FloridaSenateRegistrationError, match="previously observed"):
        registration.register_reviewed_fl_senate_target(db_session)
    with pytest.raises(registration.FloridaSenateActivationError, match="previously observed"):
        registration.activate_reviewed_fl_senate_target(db_session, now=NOW)
    with pytest.raises(registration.FloridaSenateActivationError, match="timezone-aware"):
        registration.activate_reviewed_fl_senate_target(db_session, now=NOW.replace(tzinfo=None))
