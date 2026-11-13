"""Deterministic server-owned resource selection for refresh entry points."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.v2.connection import Connection
from app.models.v2.dataset import Dataset
from app.models.v2.refresh import RefreshRun, RefreshSchedule, RefreshSourceState
from app.schemas.refresh import RefreshError, RefreshPolicy
from app.services.v2.incremental.operations import resolve_refresh_resource
from app.services.v2.scheduler.schedule_service import dispatch_due_schedules, upsert_refresh_schedule, ScheduleRequest
from app.tasks.v2.refresh_tasks import create_manual_connection_run


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _connection(db, connection_id="resource-source", config=None):
    connection = Connection(
        id=connection_id,
        name=connection_id,
        kind="rest",
        config=config or {},
        status="active",
        refresh_policy=RefreshPolicy.BATCH.value,
        cursor_contract="watermark_primary_key",
    )
    db.add(connection)
    db.commit()
    return connection


def test_resource_config_wins_over_ambiguous_states(db):
    _connection(db, config={"resource": "orders"})
    db.add_all([
        RefreshSourceState(
            id="state-users", source_id="resource-source", resource="users",
            cursor_contract="watermark_primary_key", config_version=1,
            fencing_token=0,
        ),
        RefreshSourceState(
            id="state-events", source_id="resource-source", resource="events",
            cursor_contract="watermark_primary_key", config_version=1,
            fencing_token=0,
        ),
    ])
    db.commit()

    assert resolve_refresh_resource(db, source_id="resource-source") == "orders"


def test_resource_resolution_ignores_legacy_sentinel_and_uses_one_real_state(db):
    _connection(db)
    db.add(RefreshSourceState(
        id="state-sentinel", source_id="resource-source", resource="__scheduled__",
        cursor_contract="watermark_primary_key", config_version=1, fencing_token=0,
    ))
    db.commit()

    with pytest.raises(RefreshError) as exc_info:
        resolve_refresh_resource(db, source_id="resource-source")
    assert exc_info.value.reason_code == "REFRESH_RESOURCE_REQUIRED"

    db.add(RefreshSourceState(
        id="state-orders", source_id="resource-source", resource="orders",
        cursor_contract="watermark_primary_key", config_version=1, fencing_token=0,
    ))
    db.commit()
    assert resolve_refresh_resource(db, source_id="resource-source") == "orders"


def test_resource_resolution_uses_one_dataset_when_state_is_absent(db):
    _connection(db)
    db.add(Dataset(
        id="dataset-orders", name="orders", source_connection_id="resource-source",
        kind="structured",
    ))
    db.commit()

    assert resolve_refresh_resource(db, source_id="resource-source") == "orders"


def test_resource_resolution_rejects_ambiguous_state_candidates(db):
    _connection(db)
    db.add_all([
        RefreshSourceState(
            id="state-orders", source_id="resource-source", resource="orders",
            cursor_contract="watermark_primary_key", config_version=1, fencing_token=0,
        ),
        RefreshSourceState(
            id="state-users", source_id="resource-source", resource="users",
            cursor_contract="watermark_primary_key", config_version=1, fencing_token=0,
        ),
    ])
    db.commit()

    with pytest.raises(RefreshError) as exc_info:
        resolve_refresh_resource(db, source_id="resource-source")
    assert exc_info.value.reason_code == "REFRESH_RESOURCE_AMBIGUOUS"


def test_manual_compatibility_run_uses_concrete_resolved_resource(db):
    _connection(db, config={"resource": "orders"})

    run = create_manual_connection_run(db, "resource-source")

    assert run.resource == "orders"
    assert db.query(RefreshRun).filter(
        RefreshRun.source_id == "resource-source", RefreshRun.resource == "__scheduled__",
    ).count() == 0


def test_due_source_schedule_uses_concrete_resolved_resource(db):
    _connection(db, config={"resource": "orders"})
    schedule = upsert_refresh_schedule(
        db,
        ScheduleRequest(
            target_type="connection", target_id="resource-source", cron_expr="* * * * *",
            timezone="UTC", business_calendar=[], sla_seconds=0, retry_policy=None,
            backfill_window_seconds=0, max_pending_runs=1, enabled=True,
        ),
        now=NOW - timedelta(minutes=1),
    )

    sent = []
    dispatched = dispatch_due_schedules(
        db, now=NOW, lease_owner="beat", send_refresh=lambda message: sent.append(message) or message.run_id,
    )

    run = db.query(RefreshRun).filter(RefreshRun.idempotency_key.like(f"schedule:{schedule.id}:%")).one()
    assert dispatched == [run.id]
    assert run.resource == "orders"
    assert run.resource != "__scheduled__"
    assert sent[0].task_name == "refresh.connection"


def test_pipeline_target_schedule_is_rejected_before_persistence(db):
    from app.services.v2.scheduler.cron_service import CronService

    with pytest.raises(ValueError, match="PIPELINE_SCHEDULE_UNSUPPORTED"):
        CronService().schedule_pipeline_run("pipeline-1", "*/30 * * * *", db=db)

    assert db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "pipeline", RefreshSchedule.target_id == "pipeline-1",
    ).count() == 0


def test_existing_pipeline_schedule_is_disabled_without_poll_run(db):
    schedule = RefreshSchedule(
        id="pipeline-schedule-1", target_type="pipeline", target_id="pipeline-1",
        cron_expression="* * * * *", timezone="UTC", excluded_dates=[],
        max_pending_runs=1, enabled=True, next_due_at=NOW,
    )
    db.add(schedule)
    db.commit()

    assert dispatch_due_schedules(
        db, now=NOW, lease_owner="beat", send_refresh=lambda message: message.run_id,
    ) == []
    db.refresh(schedule)
    assert schedule.enabled is False
    assert db.query(RefreshRun).filter(RefreshRun.source_id == "pipeline-1").count() == 0


def test_bad_resource_schedule_does_not_block_good_schedule(db):
    _connection(db, connection_id="good-resource-source", config={"resource": "orders"})
    _connection(db, connection_id="bad-resource-source")
    good = upsert_refresh_schedule(
        db,
        ScheduleRequest(
            target_type="connection", target_id="good-resource-source", cron_expr="* * * * *",
            timezone="UTC", business_calendar=[], sla_seconds=0, retry_policy=None,
            backfill_window_seconds=0, max_pending_runs=1, enabled=True,
        ),
        now=NOW - timedelta(minutes=1),
    )
    bad = upsert_refresh_schedule(
        db,
        ScheduleRequest(
            target_type="connection", target_id="bad-resource-source", cron_expr="* * * * *",
            timezone="UTC", business_calendar=[], sla_seconds=0, retry_policy=None,
            backfill_window_seconds=0, max_pending_runs=1, enabled=True,
        ),
        now=NOW - timedelta(minutes=1),
    )

    dispatched = dispatch_due_schedules(
        db, now=NOW, lease_owner="beat", send_refresh=lambda message: message.run_id,
    )

    assert len(dispatched) == 1
    run = db.query(RefreshRun).filter(RefreshRun.idempotency_key.like(f"schedule:{good.id}:%")).one()
    assert dispatched == [run.id]
    db.refresh(bad)
    assert bad.next_due_at.replace(tzinfo=timezone.utc) > NOW
