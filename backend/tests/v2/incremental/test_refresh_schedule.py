"""Persisted T+1/cron schedule + beat-dispatch tests (Task 7).

`db` (SQLite, function-scoped, autoused table creation) comes from the
top-level tests/conftest.py, matching test_refresh_contract.py's pattern.

Two of the four literal assertions in the amended plan (`test_
due_schedule_is_dispatched_once_after_persistent_claim`) referenced
hardcoded run IDs ("run-001"/"run-002"). Production run IDs are uuid4
(matching app.services.v2.incremental.contract's `_new_id` convention, and
this module's own `schedule_service._new_id`) — there is no deterministic
"run-001" scheme anywhere else in this codebase, and inventing one just to
satisfy a literal string would be a real, unjustified deviation from how
every other RefreshRun gets its id. This file preserves the exact behavior
those assertions describe (dispatched exactly once; the second call at the
same `now` dispatches nothing; the message carries only run_id/task_name/
queue) but compares against the actually-created run's id, fetched from the
database, instead of a hardcoded literal.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.v2.connection import Connection
from app.models.v2.refresh import RefreshRun, RefreshSchedule
from app.services.v2.scheduler.schedule_service import (
    ScheduleRequest,
    dispatch_due_schedules,
    upsert_refresh_schedule,
)

FIXED_NOW = datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc)


# ── fixture helpers ─────────────────────────────────────────────────────

def upsert_fixture_schedule(db, *, cron_expr, timezone, business_calendar, sla_seconds,
                            backfill_window_seconds, max_pending_runs, target_id="source-tplusone",
                            enabled=True) -> RefreshSchedule:
    request = ScheduleRequest(
        target_type="connection", target_id=target_id, cron_expr=cron_expr, timezone=timezone,
        business_calendar=business_calendar, sla_seconds=sla_seconds, retry_policy=None,
        backfill_window_seconds=backfill_window_seconds, max_pending_runs=max_pending_runs, enabled=enabled,
    )
    return upsert_refresh_schedule(db, request, now=FIXED_NOW)


def create_fixture_schedule(db, *, target_id, max_pending_runs=5, cron_expr="* * * * *", retry_policy=None) -> RefreshSchedule:
    """A schedule already due at FIXED_NOW (every-minute cron, upserted one
    minute before FIXED_NOW so next_due_at lands exactly on it)."""
    if db.get(Connection, target_id) is None:
        db.add(Connection(
            id=target_id, name=target_id, kind="rest", config={"resource": "orders"},
            status="active", refresh_policy="batch", cursor_contract="watermark_primary_key",
        ))
        db.commit()
    request = ScheduleRequest(
        target_type="connection", target_id=target_id, cron_expr=cron_expr, timezone="UTC",
        business_calendar=[], sla_seconds=0, retry_policy=retry_policy, backfill_window_seconds=0,
        max_pending_runs=max_pending_runs, enabled=True,
    )
    return upsert_refresh_schedule(db, request, now=FIXED_NOW - timedelta(minutes=1))


def create_queued_fixture_run(db, *, source_id, resource="orders") -> RefreshRun:
    """A pre-existing active run occupying the source's admission slot,
    independent of any schedule dispatch."""
    run = RefreshRun(
        id=str(uuid.uuid4()), source_id=source_id, resource=resource, policy="batch",
        trigger="manual", config_version=1, cursor_contract="watermark_primary_key",
        status="queued", dispatch_state="dispatched", idempotency_key=f"fixture:{uuid.uuid4()}",
        fencing_token=0, retry_count=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def latest_fixture_run(db, *, source_id) -> RefreshRun:
    """The most recent schedule-dispatch-created run for `source_id`,
    identified by the "schedule:" idempotency-key prefix `_dispatch_one_
    schedule` assigns — avoids relying on wall-clock created_at ordering to
    tell it apart from any pre-existing fixture run for the same source."""
    return db.execute(
        select(RefreshRun)
        .where(RefreshRun.source_id == source_id, RefreshRun.idempotency_key.like("schedule:%"))
        .order_by(RefreshRun.created_at.desc())
    ).scalars().first()


# ── tests ────────────────────────────────────────────────────────────────

def test_t_plus_one_schedule_persists_timezone_calendar_sla_and_retry(db):
    schedule = upsert_fixture_schedule(db, cron_expr="0 2 * * *", timezone="Asia/Shanghai",
                                       business_calendar=["2026-10-01"], sla_seconds=86400,
                                       backfill_window_seconds=172800, max_pending_runs=2)
    assert schedule.timezone == "Asia/Shanghai"
    assert schedule.business_calendar == ["2026-10-01"]
    assert schedule.sla_seconds == 86400
    assert schedule.max_pending_runs == 2
    assert schedule.next_due_at.tzinfo is not None


def test_due_schedule_is_dispatched_once_after_persistent_claim(db):
    create_fixture_schedule(db, target_id="source-001")
    sent = []
    ids = dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-a",
                                 send_refresh=lambda message: sent.append(message.to_dict()) or message.run_id)
    run_id = latest_fixture_run(db, source_id="source-001").id
    assert ids == [run_id]
    assert sent == [{"run_id": run_id, "task_name": "refresh.connection", "queue": "refresh.poll"}]
    assert dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-b",
                                  send_refresh=lambda _message: "run-002") == []


def test_scheduled_run_persists_destination_queue_for_operability(db):
    create_fixture_schedule(db, target_id="source-queue-attribution")
    dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-a",
                           send_refresh=lambda message: message.run_id)

    run = latest_fixture_run(db, source_id="source-queue-attribution")
    assert run.dispatch_queue == "refresh.poll"


def test_manual_refresh_run_persists_destination_queue_for_operability(db):
    from app.tasks.v2.refresh_tasks import create_manual_connection_run

    db.add(Connection(
        id="source-manual-queue-attribution", name="manual source", kind="rest",
        config={"resource": "orders"}, status="active", refresh_policy="batch",
        cursor_contract="watermark_primary_key",
    ))
    db.commit()
    run = create_manual_connection_run(db, "source-manual-queue-attribution")
    assert run.dispatch_queue == "refresh.poll"


def test_beat_has_real_refresh_dispatch_entry():
    from app.tasks.celery_app import celery_app

    assert celery_app.conf.beat_schedule["refresh-schedule-dispatch"]["task"] == "refresh.dispatch_due_schedules"


def test_saturated_source_is_queued_with_backpressure_and_never_pulled(db):
    create_fixture_schedule(db, target_id="source-001", max_pending_runs=1)
    create_queued_fixture_run(db, source_id="source-001")
    sent = []
    ids = dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-a",
                                 send_refresh=lambda message: sent.append(message.to_dict()) or "broker-id")
    assert ids == []
    assert sent == []
    assert latest_fixture_run(db, source_id="source-001").dispatch_state == "backpressured"
    assert latest_fixture_run(db, source_id="source-001").status == "queued"


def test_saturated_beats_reuse_one_backpressured_run_until_capacity_recovers(db):
    create_fixture_schedule(db, target_id="source-bounded", max_pending_runs=1)
    blocker = create_queued_fixture_run(db, source_id="source-bounded")
    sent = []

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-a",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == []
    retained = latest_fixture_run(db, source_id="source-bounded")
    assert retained.dispatch_state == "backpressured"

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-b",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == []
    assert db.execute(
        select(func.count()).select_from(RefreshRun).where(RefreshRun.source_id == "source-bounded")
    ).scalar_one() == 2
    assert latest_fixture_run(db, source_id="source-bounded").id == retained.id
    assert sent == []

    blocker.status = "succeeded"
    db.commit()
    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-c",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == [retained.id]
    assert sent == [retained.id]
    assert db.get(RefreshRun, retained.id).dispatch_state == "dispatched"

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-d",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == []
    assert sent == [retained.id]
    assert db.execute(
        select(func.count()).select_from(RefreshRun).where(RefreshRun.source_id == "source-bounded")
    ).scalar_one() == 2


def test_publish_failed_occurrence_is_retried_without_duplicate(db):
    create_fixture_schedule(db, target_id="source-publish-retry")
    attempts = []

    def fail_once(message):
        attempts.append(message.run_id)
        raise RuntimeError("broker unavailable")

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-a", send_refresh=fail_once,
    ) == []
    retained = latest_fixture_run(db, source_id="source-publish-retry")
    assert retained.dispatch_state == "publish_failed"
    assert attempts == [retained.id]

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-b",
        send_refresh=lambda message: attempts.append(message.run_id) or "broker-id",
    ) == [retained.id]
    assert attempts == [retained.id, retained.id]
    assert db.get(RefreshRun, retained.id).dispatch_state == "dispatched"

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="beat-c",
        send_refresh=lambda message: attempts.append(message.run_id) or "broker-id",
    ) == []
    assert attempts == [retained.id, retained.id]
    assert db.execute(
        select(func.count()).select_from(RefreshRun).where(RefreshRun.source_id == "source-publish-retry")
    ).scalar_one() == 1


def test_concurrent_like_dispatchers_cannot_publish_same_claimed_occurrence(db):
    create_fixture_schedule(db, target_id="source-concurrent-claim")
    first_sent = []
    concurrent_sent = []

    def publish_first(message):
        # Deterministically interleave a second beat while the first callback
        # is still running. The persisted claim must block the nested publish.
        nested_ids = dispatch_due_schedules(
            db, now=FIXED_NOW, lease_owner="beat-b",
            send_refresh=lambda nested: concurrent_sent.append(nested.run_id) or "nested-broker-id",
        )
        assert nested_ids == []
        first_sent.append(message.run_id)
        return "first-broker-id"

    ids = dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-a", send_refresh=publish_first)
    assert ids == first_sent
    assert concurrent_sent == []


def test_publish_retry_policy_applies_backoff_and_stops_at_max_attempts(db):
    create_fixture_schedule(
        db, target_id="source-retry-policy", retry_policy={"max_attempts": 2, "backoff_seconds": 60},
    )
    attempts = []

    def fail_publish(message):
        attempts.append(message.run_id)
        raise RuntimeError("broker unavailable")

    assert dispatch_due_schedules(db, now=FIXED_NOW, lease_owner="beat-a", send_refresh=fail_publish) == []
    retained = latest_fixture_run(db, source_id="source-retry-policy")
    assert retained.retry_count == 1
    retry_at = retained.dispatch_retry_at
    if retry_at.tzinfo is None:  # SQLite has no timezone-aware datetime type.
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    assert retry_at == FIXED_NOW + timedelta(seconds=60)

    assert dispatch_due_schedules(
        db, now=FIXED_NOW + timedelta(seconds=59), lease_owner="beat-b", send_refresh=fail_publish,
    ) == []
    assert attempts == [retained.id]

    assert dispatch_due_schedules(
        db, now=FIXED_NOW + timedelta(seconds=60), lease_owner="beat-c", send_refresh=fail_publish,
    ) == []
    assert attempts == [retained.id, retained.id]
    assert retained.retry_count == 2
    assert retained.dispatch_retry_at is None
    assert retained.dispatch_reason == "RETRY_EXHAUSTED"

    assert dispatch_due_schedules(
        db, now=FIXED_NOW + timedelta(hours=1), lease_owner="beat-d", send_refresh=fail_publish,
    ) == []
    assert attempts == [retained.id, retained.id]


def test_retry_policy_backoff_has_explicit_upper_bound(db):
    with pytest.raises(ValueError, match="backoff_seconds"):
        create_fixture_schedule(
            db, target_id="source-retry-bound", retry_policy={"max_attempts": 2, "backoff_seconds": 3601},
        )


def test_expired_dispatch_claim_is_recovered_once_without_duplicate(db):
    schedule = create_fixture_schedule(db, target_id="source-expired-claim")
    occurrence_key = f"schedule:{schedule.id}:{schedule.next_due_at.isoformat()}"
    retained = RefreshRun(
        id=str(uuid.uuid4()), source_id="source-expired-claim", resource="orders",
        policy="batch", trigger="scheduled", config_version=1,
        cursor_contract="watermark_primary_key", status="queued", dispatch_state="pending",
        dispatch_queue="refresh.poll", dispatch_claim_owner="abandoned-beat",
        dispatch_claim_expires_at=FIXED_NOW - timedelta(seconds=1), dispatch_retry_at=None,
        idempotency_key=occurrence_key, fencing_token=0, retry_count=0,
    )
    db.add(retained)
    schedule.last_dispatched_run_id = retained.id
    db.commit()

    sent = []
    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="recovery-beat",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == [retained.id]
    assert sent == [retained.id]
    assert db.get(RefreshRun, retained.id).dispatch_state == "dispatched"
    assert db.get(RefreshRun, retained.id).dispatch_claim_owner is None

    assert dispatch_due_schedules(
        db, now=FIXED_NOW, lease_owner="follow-up-beat",
        send_refresh=lambda message: sent.append(message.run_id) or "broker-id",
    ) == []
    assert sent == [retained.id]
