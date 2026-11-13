"""Celery refresh tasks (Tasks 7-8) — beat-driven schedule dispatch and the
run-ID-only polling worker.

`dispatch_due_schedules_task` (`refresh.schedule` queue) is the only Celery
entry point that turns a persisted `RefreshSchedule` into a durable
`RefreshRun` and hands its `run_id` to the broker — see
`app.services.v2.scheduler.schedule_service`. `refresh_poll_task` is the
sole run-ID-only polling worker on `refresh.poll`; the compatibility
`refresh.connection`/`refresh.pipeline` names delegate to it for persisted
schedule messages.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import SoftTimeLimitExceeded, WorkerShutdown

from app.tasks.celery_app import celery_app
from app.tasks.topology import (
    QUEUE_REFRESH_EVENT,
    QUEUE_REFRESH_POLL,
    build_refresh_dispatch_message,
    enqueue_refresh_run,
)

logger = logging.getLogger(__name__)

def _celery_send_task(task_name: str, args, queue: str) -> str:
    """Adapter matching `enqueue_refresh_run`'s `send_task` contract —
    Celery's own `send_task(name, args, kwargs, ...)` would otherwise bind a
    positional third argument to `kwargs`, not `queue`."""
    result = celery_app.send_task(task_name, args=list(args), queue=queue)
    return result.id


def _send_refresh_via_celery(message) -> str:
    return enqueue_refresh_run(message=message, send_task=_celery_send_task)


def _send_event_via_celery(run_id: str):
    """Publish only a durable event run ID during inbox reconciliation."""
    message = build_refresh_dispatch_message(run_id=run_id, task_name="refresh.event")
    return enqueue_refresh_run(message=message, send_task=_celery_send_task)


def _mark_refresh_dequeue(queue: str, run_id: str) -> None:
    from app.services.v2.incremental.broker_observer import record_refresh_dequeue

    record_refresh_dequeue(queue=queue, run_id=run_id)


@celery_app.task(name="refresh.dispatch_due_schedules")
def dispatch_due_schedules_task() -> list[str]:
    """Beat-driven: publish every due persisted schedule's run to the broker.
    Never pulls a connector itself — only claims/creates the durable
    `RefreshRun` and hands its run_id to `refresh.connection`/`refresh.pipeline`."""
    import app.models  # noqa: F401 — register all tables for the worker subprocess
    from app.database import SessionLocal
    from app.services.v2.scheduler.schedule_service import dispatch_due_schedules

    db = SessionLocal()
    try:
        schedule_run_ids = dispatch_due_schedules(
            db, now=datetime.now(timezone.utc), lease_owner="beat",
            send_refresh=_send_refresh_via_celery,
        )
        # Beat is also the durable recovery loop for managed events.  This
        # scans rows accepted while the broker/source lease was unavailable;
        # each callback still carries only the run ID to refresh.event.
        from app.services.v2.incremental.event_ingest import EventIngestService

        EventIngestService().drain_pending(
            db, dispatch=_send_event_via_celery, lease_owner="event-dispatcher:beat",
            now=datetime.now(timezone.utc),
        )
        return schedule_run_ids
    finally:
        db.close()


def _refresh_poll(run_id: str) -> dict:
    """Execute one durable run, acknowledging only after its transition."""
    import app.models  # noqa: F401
    from app.database import SessionLocal
    from app.models.v2.refresh import RefreshRun
    from app.schemas.refresh import ConfigurationDriftError, RefreshCancellationRequested
    from app.services.v2.incremental.contract import (
        finalize_refresh_cancellation,
        mark_refresh_retryable,
    )
    from app.services.v2.incremental.polling import poll_source

    db = SessionLocal()
    try:
        run = db.get(RefreshRun, run_id)
        if run is None:
            return {"run_id": run_id, "status": "missing"}
        owner = run.lease_owner or f"refresh-worker:{uuid.uuid4()}"
        try:
            result = poll_source(
                db,
                source_id=run.source_id,
                resource=run.resource,
                lease_owner=owner,
                now=datetime.now(timezone.utc),
                _run_id=run.id,
            )
            return result.to_dict()
        except RefreshCancellationRequested:
            db.rollback()
            current = db.get(RefreshRun, run_id)
            finalized = finalize_refresh_cancellation(
                db,
                run_id=run_id,
                lease_owner=current.lease_owner or owner,
                fencing_token=current.fencing_token,
                now=datetime.now(timezone.utc),
            )
            return {"run_id": finalized.id, "status": finalized.status}
        except SoftTimeLimitExceeded:
            db.rollback()
            retried = mark_refresh_retryable(
                db, run_id=run_id, reason="WORKER_INTERRUPTED", now=datetime.now(timezone.utc),
            )
            return {"run_id": retried.id, "status": retried.status, "retry_count": retried.retry_count}
        except (WorkerShutdown, KeyboardInterrupt, SystemExit):
            db.rollback()
            retried = mark_refresh_retryable(
                db, run_id=run_id, reason="WORKER_INTERRUPTED", now=datetime.now(timezone.utc),
            )
            return {"run_id": retried.id, "status": retried.status, "retry_count": retried.retry_count}
        except ConfigurationDriftError as exc:
            # A stale run is failed without touching source cursor or lineage;
            # the typed reason remains visible to the worker caller.
            db.rollback()
            current = db.get(RefreshRun, run_id)
            from sqlalchemy import select
            from app.models.v2.refresh import RefreshSourceState

            source_state = db.execute(
                select(RefreshSourceState).where(
                    RefreshSourceState.source_id == current.source_id,
                    RefreshSourceState.resource == current.resource,
                ).with_for_update()
            ).scalar_one_or_none()
            if (
                source_state is not None
                and current.lease_owner is not None
                and source_state.lease_owner == current.lease_owner
                and source_state.fencing_token == current.fencing_token
            ):
                # The source state is authoritative. Clear only the stale
                # run's matching ownership; never release a newer fence.
                source_state.lease_owner = None
                source_state.lease_expires_at = None
                source_state.updated_at = datetime.now(timezone.utc)
            current.status = "failed"
            current.retry_reason = exc.reason_code
            current.terminal_at = datetime.now(timezone.utc)
            current.lease_owner = None
            current.lease_expires_at = None
            from app.models.v2.refresh import RefreshRunTransition

            current_transition = RefreshRunTransition(
                id=str(uuid.uuid4()), run_id=current.id, from_status="running",
                to_status="failed", reason=exc.reason_code, actor=owner,
            )
            db.add(current_transition)
            db.commit()
            return {"run_id": current.id, "status": current.status, "error_code": exc.reason_code}
    finally:
        db.close()


@celery_app.task(name="refresh.poll")
def refresh_poll_task(run_id: str) -> dict:
    """Run-ID-only polling worker entry."""
    _mark_refresh_dequeue(QUEUE_REFRESH_POLL, run_id)
    return _refresh_poll(run_id)


def _refresh_event(run_id: str) -> dict:
    """Execute one durable inbox event; the worker receives only ``run_id``."""
    import app.models  # noqa: F401
    from app.database import SessionLocal
    from app.models.v2.refresh import RefreshRun, RefreshRunTransition, RefreshSourceState
    from app.schemas.refresh import ConfigurationDriftError, RefreshCancellationRequested
    from app.services.v2.incremental.contract import finalize_refresh_cancellation, mark_refresh_retryable
    from app.services.v2.incremental.event_ingest import EventIngestService
    from sqlalchemy import select

    db = SessionLocal()
    try:
        run = db.get(RefreshRun, run_id)
        if run is None:
            return {"run_id": run_id, "status": "missing"}
        owner = run.lease_owner or f"refresh-event-worker:{uuid.uuid4()}"
        try:
            result = EventIngestService().process(
                db, run_id=run_id, lease_owner=owner, now=datetime.now(timezone.utc),
            )
            return result.to_dict()
        except RefreshCancellationRequested:
            db.rollback()
            current = db.get(RefreshRun, run_id)
            finalized = finalize_refresh_cancellation(
                db, run_id=run_id, lease_owner=current.lease_owner or owner,
                fencing_token=current.fencing_token, now=datetime.now(timezone.utc),
            )
            return {"run_id": finalized.id, "status": finalized.status}
        except SoftTimeLimitExceeded:
            db.rollback()
            retried = mark_refresh_retryable(
                db, run_id=run_id, reason="WORKER_INTERRUPTED", now=datetime.now(timezone.utc),
            )
            return {"run_id": retried.id, "status": retried.status, "retry_count": retried.retry_count}
        except (WorkerShutdown, KeyboardInterrupt, SystemExit):
            db.rollback()
            retried = mark_refresh_retryable(
                db, run_id=run_id, reason="WORKER_INTERRUPTED", now=datetime.now(timezone.utc),
            )
            return {"run_id": retried.id, "status": retried.status, "retry_count": retried.retry_count}
        except ConfigurationDriftError as exc:
            # The frozen revision is authoritative.  Release only this run's
            # matching ownership and leave the source cursor/lineage untouched.
            db.rollback()
            current = db.get(RefreshRun, run_id)
            source_state = db.execute(
                select(RefreshSourceState).where(
                    RefreshSourceState.source_id == current.source_id,
                    RefreshSourceState.resource == current.resource,
                ).with_for_update()
            ).scalar_one_or_none()
            if (
                source_state is not None
                and current.lease_owner is not None
                and source_state.lease_owner == current.lease_owner
                and source_state.fencing_token == current.fencing_token
            ):
                source_state.lease_owner = None
                source_state.lease_expires_at = None
                source_state.updated_at = datetime.now(timezone.utc)
            old_status = current.status
            current.status = "failed"
            current.retry_reason = exc.reason_code
            current.terminal_at = datetime.now(timezone.utc)
            current.lease_owner = None
            current.lease_expires_at = None
            db.add(RefreshRunTransition(
                id=str(uuid.uuid4()), run_id=current.id, from_status=old_status,
                to_status="failed", reason=exc.reason_code, actor=owner,
            ))
            db.commit()
            return {"run_id": current.id, "status": current.status, "error_code": exc.reason_code}
    finally:
        db.close()


@celery_app.task(name="refresh.event")
def refresh_event_task(run_id: str) -> dict:
    """Run-ID-only managed webhook/outbox worker entry."""
    _mark_refresh_dequeue(QUEUE_REFRESH_EVENT, run_id)
    return _refresh_event(run_id)


@celery_app.task(name="refresh.replay")
def refresh_replay_task(run_id: str) -> dict:
    """Run-ID-only replay worker entry.

    The retained dead-letter/inbox record (when present) is reloaded by the
    event worker; a failed polling run is reloaded by the polling worker.
    Neither path accepts operator-supplied payload, cursor, credential, or
    connector configuration.
    """
    from app.services.v2.incremental.broker_observer import record_refresh_dequeue
    from app.tasks.topology import QUEUE_REFRESH_REPLAY

    record_refresh_dequeue(queue=QUEUE_REFRESH_REPLAY, run_id=run_id)
    import app.models  # noqa: F401
    from app.database import SessionLocal
    from app.models.v2.refresh import RefreshInboxEvent
    from sqlalchemy import select

    db = SessionLocal()
    try:
        inbox = db.execute(
            select(RefreshInboxEvent).where(RefreshInboxEvent.run_id == run_id)
        ).scalar_one_or_none()
    finally:
        db.close()
    if inbox is not None:
        return _refresh_event(run_id)
    return _refresh_poll(run_id)


@celery_app.task(name="refresh.connection")
def refresh_connection_task(run_id: str) -> dict:
    """Compatibility task name delegating to the sole polling worker."""
    return refresh_poll_task.run(run_id)


@celery_app.task(name="refresh.pipeline")
def refresh_pipeline_task(run_id: str) -> dict:
    """Compatibility task name delegating to the sole polling worker."""
    return refresh_poll_task.run(run_id)


# ── manual on-demand trigger (shared by the /connections/{id}/sync route and
# the sync_connection/sync_all_connections compatibility wrappers) ─────────

def create_manual_connection_run(db, connection_id: str):
    """Claim a durable manual run using the persisted connection policy.

    This compatibility helper still produces a leased run for the legacy
    `refresh.connection` task, but it rejects event-driven sources before any
    claim so they cannot be forced onto `refresh.poll`.
    """
    from app.models.v2.connection import Connection
    from app.schemas.refresh import RefreshError, RefreshPolicy
    from app.services.v2.incremental.operations import (
        _policy_for_source,
        _source_state,
        resolve_refresh_resource,
    )
    from app.services.v2.incremental.contract import claim_refresh_run

    connection = db.get(Connection, connection_id)
    if connection is None:
        raise RefreshError("SOURCE_NOT_FOUND", f"no source connection {connection_id}")

    resource = resolve_refresh_resource(db, source_id=connection_id)
    configured_state = _source_state(db, source_id=connection_id, resource=resource)
    policy = _policy_for_source(connection, configured_state)
    if policy is RefreshPolicy.EVENT_DRIVEN:
        raise RefreshError(
            "EVENT_TRIGGER_REQUIRES_SIGNATURE",
            "event-driven refreshes require the signed webhook route",
        )

    run = claim_refresh_run(
        db, source_id=connection_id, resource=resource, policy=policy,
        idempotency_key=f"manual:{uuid.uuid4()}", lease_owner="manual-sync",
        now=datetime.now(timezone.utc),
    )
    run.dispatch_queue = QUEUE_REFRESH_POLL
    db.commit()
    db.refresh(run)
    return run


def trigger_connection_refresh(connection_id: str) -> dict:
    """sync_connection/sync_all_connections compatibility wrapper: claim a
    manual run and dispatch it through `refresh.connection` on `refresh.poll`."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        run = create_manual_connection_run(db, connection_id)
    finally:
        db.close()

    try:
        refresh_connection_task.delay(run.id)
        return {"connection_id": connection_id, "run_id": run.id, "status": "queued"}
    except Exception as exc:
        return {"connection_id": connection_id, "run_id": run.id, "status": "dispatch_failed", "error": str(exc)}
