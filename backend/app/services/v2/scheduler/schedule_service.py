"""Persisted T+1/cron refresh schedules (Task 7).

Owns `RefreshSchedule` persistence and the business-calendar/timezone-aware
"next due instant" calculation, and is the only place that turns a due
schedule into a durable `RefreshRun` and hands it to a broker. Validation
alone (`upsert_refresh_schedule` rejecting a bad request) never marks a
schedule active on its own — a schedule only becomes "scheduled" once its row
is persisted and `next_due_at` is computed; and a schedule only ever
dispatches through `dispatch_due_schedules`, never inline from an API
request or from Celery beat itself pulling a connector.

`dispatch_due_schedules` creates its `RefreshRun`s directly (status=
"queued", no lease) rather than reusing `app.services.v2.incremental.
contract.claim_refresh_run` (Task 6): that function immediately marks a run
"running" with an active lease, which fits a connector claiming work it is
about to execute synchronously, not a scheduler that only hands a durable
run_id to Celery for a worker to pick up later. The run only starts
"running" once a worker actually claims it (a later task's concern).
"""
from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.v2.refresh import RefreshRun, RefreshSchedule, RefreshSourceState
from app.schemas.refresh import RefreshPolicy
from app.services.v2.scheduler.cron_service import CronService
from app.tasks.topology import (
    QUEUE_REFRESH_POLL,
    RefreshDispatchMessage,
    build_refresh_dispatch_message,
)

_cron_service = CronService()
logger = logging.getLogger(__name__)

# ScheduleRequest.target_type is the API-facing vocabulary ("connection" /
# "pipeline"); RefreshSchedule.target_type persists the same vocabulary the
# rest of the durable refresh contract uses ("source" / "pipeline" — see
# app/models/v2/refresh.py's ck_refresh_schedules_target_type and how
# RefreshSourceState/RefreshRun key everything off `source_id`).
_STORED_TARGET_TYPE = {"connection": "source", "pipeline": "pipeline"}
_TASK_NAME_BY_STORED_TARGET_TYPE = {"source": "refresh.connection", "pipeline": "refresh.pipeline"}

# Retained as an import-compatible fixture value for older callers.  New
# schedule dispatch must resolve a concrete resource before creating a run.
DEFAULT_RESOURCE = "__scheduled__"

PIPELINE_SCHEDULE_UNSUPPORTED = "PIPELINE_SCHEDULE_UNSUPPORTED"

_MAX_RETRY_ATTEMPTS = 20
_DEFAULT_MAX_ATTEMPTS = 3
_MAX_RETRY_BACKOFF_SECONDS = 3600
_DISPATCH_CLAIM_LEASE_SECONDS = 60
_BACKPRESSURE_REASON = "BACKPRESSURE"
_PUBLISH_FAILED_REASON = "PUBLISH_FAILED"
_RETRY_EXHAUSTED_REASON = "RETRY_EXHAUSTED"


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ScheduleRequest:
    target_type: str  # "connection" | "pipeline"
    target_id: str
    cron_expr: str
    timezone: str
    business_calendar: Sequence[str]
    sla_seconds: int
    retry_policy: Mapping[str, object] | None
    backfill_window_seconds: int
    max_pending_runs: int
    enabled: bool


# ── validation ───────────────────────────────────────────────────────────

def _expand_cron_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"invalid cron step: {part}/{step}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(part)
        if start > end or start < lo or end > hi:
            raise ValueError(f"cron field value out of range [{lo},{hi}]: {part}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron field expands to no values: {field}")
    return values


def _validate_cron_field_ranges(cron_expr: str) -> None:
    minute_f, hour_f, dom_f, moy_f, dow_f = cron_expr.strip().split()
    _expand_cron_field(minute_f, 0, 59)
    _expand_cron_field(hour_f, 0, 23)
    _expand_cron_field(dom_f, 1, 31)
    _expand_cron_field(moy_f, 1, 12)
    _expand_cron_field(dow_f, 0, 7)


def _validate_retry_policy(retry_policy: Mapping[str, object] | None) -> None:
    if retry_policy is None:
        return
    max_attempts = retry_policy.get("max_attempts")
    if max_attempts is None or not (1 <= int(max_attempts) <= _MAX_RETRY_ATTEMPTS):
        raise ValueError(f"retry_policy.max_attempts must be between 1 and {_MAX_RETRY_ATTEMPTS}")
    backoff_seconds = retry_policy.get("backoff_seconds", 0)
    if not (0 <= int(backoff_seconds) <= _MAX_RETRY_BACKOFF_SECONDS):
        raise ValueError(
            "retry_policy.backoff_seconds must be between "
            f"0 and {_MAX_RETRY_BACKOFF_SECONDS}"
        )


def _validate_request(request: ScheduleRequest) -> ZoneInfo:
    if request.target_type not in _STORED_TARGET_TYPE:
        raise ValueError(f"unsupported target_type: {request.target_type!r}")
    if request.target_type == "pipeline":
        raise ValueError(
            f"{PIPELINE_SCHEDULE_UNSUPPORTED}: pipeline-target schedules are not supported"
        )
    if not request.target_id:
        raise ValueError("target_id is required")
    if not _cron_service.validate_cron(request.cron_expr):
        raise ValueError(f"invalid cron expression: {request.cron_expr!r}")
    _validate_cron_field_ranges(request.cron_expr)
    try:
        tz = ZoneInfo(request.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {request.timezone!r}") from exc
    if request.sla_seconds < 0:
        raise ValueError("sla_seconds must be non-negative")
    if request.backfill_window_seconds < 0:
        raise ValueError("backfill_window_seconds must be non-negative")
    if request.max_pending_runs <= 0:
        raise ValueError("max_pending_runs must be positive")
    _validate_retry_policy(request.retry_policy)
    return tz


# ── next-fire (T+1/cron) calculation ────────────────────────────────────

def _next_fire_local(cron_expr: str, tz: ZoneInfo, after: datetime) -> datetime:
    """Earliest local wall-clock instant strictly after `after` (already in
    `tz`) matching the 5-field cron expression. Skips whole non-matching
    days in one step, then scans matching-day hour/minute combinations in
    order — bounded to four years so a rare expression (e.g. Feb 29 only)
    cannot loop unboundedly."""
    minute_f, hour_f, dom_f, moy_f, dow_f = cron_expr.strip().split()
    minutes = sorted(_expand_cron_field(minute_f, 0, 59))
    hours = sorted(_expand_cron_field(hour_f, 0, 23))
    doms = _expand_cron_field(dom_f, 1, 31)
    moys = _expand_cron_field(moy_f, 1, 12)
    dows = {0 if d == 7 else d for d in _expand_cron_field(dow_f, 0, 7)}

    start = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    day_start = start.replace(hour=0, minute=0)

    for _ in range(4 * 366 + 1):
        if day_start.month in moys and day_start.day in doms and (day_start.isoweekday() % 7) in dows:
            day_floor = start if day_start.date() == start.date() else day_start
            for hour in hours:
                if hour < day_floor.hour:
                    continue
                for minute in minutes:
                    if hour == day_floor.hour and minute < day_floor.minute:
                        continue
                    return day_start.replace(hour=hour, minute=minute)
        day_start = (day_start + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError(f"no matching cron fire time found within four years for {cron_expr!r}")


def _next_fire_utc(cron_expr: str, tz: ZoneInfo, after: datetime) -> datetime:
    local_next = _next_fire_local(cron_expr, tz, after.astimezone(tz))
    return local_next.astimezone(timezone.utc)


# ── persistence ──────────────────────────────────────────────────────────

def upsert_refresh_schedule(db: Session, request: ScheduleRequest, *, now: datetime) -> RefreshSchedule:
    """Validate and persist a T+1/cron schedule, computing `next_due_at` in
    UTC. A disabled schedule is persisted with `next_due_at=None` so
    `dispatch_due_schedules` never selects it."""
    tz = _validate_request(request)
    stored_target_type = _STORED_TARGET_TYPE[request.target_type]

    schedule = db.execute(
        select(RefreshSchedule).where(
            RefreshSchedule.target_type == stored_target_type,
            RefreshSchedule.target_id == request.target_id,
        )
    ).scalar_one_or_none()
    if schedule is None:
        schedule = RefreshSchedule(id=_new_id(), target_type=stored_target_type, target_id=request.target_id)
        db.add(schedule)

    schedule.cron_expression = request.cron_expr
    schedule.timezone = request.timezone
    schedule.excluded_dates = list(request.business_calendar)
    schedule.sla_seconds = request.sla_seconds
    schedule.retry_policy = dict(request.retry_policy) if request.retry_policy else None
    schedule.backfill_window_seconds = request.backfill_window_seconds
    schedule.max_pending_runs = request.max_pending_runs
    schedule.enabled = request.enabled
    schedule.next_due_at = _next_fire_utc(request.cron_expr, tz, now) if request.enabled else None

    db.commit()
    db.refresh(schedule)
    # SQLite has no timezone-aware datetime storage and returns this persisted
    # UTC instant as naive; keep the service result's contract timezone-aware
    # across supported database dialects.
    schedule.next_due_at = _as_aware_utc(schedule.next_due_at)
    return schedule


def _current_source_revision(db: Session, *, source_id: str, resource: str) -> tuple[int, str]:
    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == source_id, RefreshSourceState.resource == resource,
        )
    ).scalar_one_or_none()
    if state is None:
        return 1, "watermark_primary_key"
    return state.config_version, state.cursor_contract


def _lock_schedule(db: Session, schedule_id: str) -> RefreshSchedule | None:
    return db.execute(
        select(RefreshSchedule).where(RefreshSchedule.id == schedule_id).with_for_update()
    ).scalar_one_or_none()


def _lock_schedule_occurrence(db: Session, *, source_id: str, resource: str,
                              idempotency_key: str) -> RefreshRun | None:
    """Return the one durable run representing this schedule occurrence.

    A saturated or publish-failed occurrence keeps the schedule due, so every
    later beat invocation must find and re-drive this row instead of creating
    another run for the same occurrence.
    """
    return db.execute(
        select(RefreshRun).where(
            RefreshRun.source_id == source_id,
            RefreshRun.resource == resource,
            RefreshRun.idempotency_key == idempotency_key,
        ).with_for_update()
    ).scalars().first()


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """SQLite's DateTime type round-trips a committed value as naive (it has
    no native tz-aware storage), even though this module always writes
    `next_due_at` in UTC — normalize back to aware UTC before comparing
    against the caller's aware `now`. A no-op against real PostgreSQL, which
    already returns a tz-aware value."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _retry_settings(schedule: RefreshSchedule) -> tuple[int, int]:
    """Return total publish attempts and base backoff for a persisted schedule.

    An omitted policy gets a finite three-attempt budget and no delay, keeping
    broker failures bounded without surprising existing schedules.
    """
    policy = schedule.retry_policy or {}
    return (
        int(policy.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)),
        int(policy.get("backoff_seconds", 0)),
    )


def _retry_at(schedule: RefreshSchedule, *, now: datetime, retry_count: int) -> datetime:
    """Compute a bounded exponential backoff for the next publish attempt."""
    _, base_seconds = _retry_settings(schedule)
    exponent = max(0, retry_count - 1)
    delay_seconds = min(_MAX_RETRY_BACKOFF_SECONDS, base_seconds * (2 ** exponent))
    return now + timedelta(seconds=delay_seconds)


def _dispatch_one_schedule(
    db: Session, *, schedule_id: str, now: datetime, lease_owner: str,
    send_refresh: Callable[[RefreshDispatchMessage], str],
) -> str | None:
    schedule = _lock_schedule(db, schedule_id)
    next_due_at = _as_aware_utc(schedule.next_due_at) if schedule is not None else None
    if schedule is None or not schedule.enabled or next_due_at is None or next_due_at > now:
        db.commit()
        return None

    tz = ZoneInfo(schedule.timezone)
    excluded = set(schedule.excluded_dates or [])

    # Skip excluded business-calendar dates, advancing next_due_at forward
    # until either a non-excluded due instant remains, or nothing is due yet.
    due_at = next_due_at
    while due_at is not None and due_at <= now and due_at.astimezone(tz).date().isoformat() in excluded:
        due_at = _next_fire_utc(schedule.cron_expression, tz, due_at)
    schedule.next_due_at = due_at

    if due_at is None or due_at > now:
        db.commit()
        return None

    if schedule.target_type == "pipeline":
        # Rows written before pipeline-target scheduling was closed remain
        # readable, but must never be turned into a polling run whose
        # ``source_id`` is actually a pipeline id.
        schedule.enabled = False
        schedule.next_due_at = None
        logger.warning(
            "Skipping unsupported pipeline refresh schedule %s (%s): %s",
            schedule.id, schedule.target_id, PIPELINE_SCHEDULE_UNSUPPORTED,
        )
        db.commit()
        return None

    source_id = schedule.target_id
    from app.services.v2.incremental.operations import resolve_refresh_resource
    from app.schemas.refresh import RefreshError

    try:
        resource = resolve_refresh_resource(db, source_id=source_id)
    except RefreshError as exc:
        logger.warning(
            "Skipping refresh schedule %s for source %s: %s",
            schedule.id, source_id, exc.reason_code,
        )
        schedule.next_due_at = _next_fire_utc(schedule.cron_expression, tz, max(due_at, now))
        db.commit()
        return None
    task_name = _TASK_NAME_BY_STORED_TARGET_TYPE["source"]
    idempotency_key = f"schedule:{schedule.id}:{due_at.isoformat()}"
    run = _lock_schedule_occurrence(
        db, source_id=source_id, resource=resource, idempotency_key=idempotency_key,
    )
    if run is None:
        config_version, cursor_contract = _current_source_revision(db, source_id=source_id, resource=resource)
    else:
        config_version, cursor_contract = run.config_version, run.cursor_contract

        retry_at = _as_aware_utc(run.dispatch_retry_at)
        if retry_at is not None and retry_at > now:
            db.commit()
            return None
        max_attempts, _ = _retry_settings(schedule)
        if run.dispatch_state == "publish_failed" and run.retry_count >= max_attempts:
            if run.dispatch_reason != _RETRY_EXHAUSTED_REASON:
                run.dispatch_reason = _RETRY_EXHAUSTED_REASON
                run.dispatch_retry_at = None
            db.commit()
            return None

        claim_expires_at = _as_aware_utc(run.dispatch_claim_expires_at)
        if claim_expires_at is not None and claim_expires_at > now:
            # Another beat owns the handoff; do not publish concurrently. An
            # expired claim is deliberately eligible for at-least-once retry.
            db.commit()
            return None

    active_query = select(func.count()).select_from(RefreshRun).where(
        RefreshRun.source_id == source_id,
        RefreshRun.resource == resource,
        RefreshRun.status.in_(("queued", "running")),
    )
    if run is not None:
        active_query = active_query.where(RefreshRun.id != run.id)
    existing_active = db.execute(active_query).scalar_one()
    admitted = existing_active < schedule.max_pending_runs

    if run is None:
        run = RefreshRun(
            id=_new_id(),
            source_id=source_id,
            resource=resource,
            policy=RefreshPolicy.BATCH.value,
            trigger="scheduled",
            config_version=config_version,
            cursor_contract=cursor_contract,
            status="queued",
            dispatch_state="pending" if admitted else "backpressured",
            dispatch_reason=None if admitted else _BACKPRESSURE_REASON,
            dispatch_queue=QUEUE_REFRESH_POLL,
            dispatch_claim_owner=lease_owner if admitted else None,
            dispatch_claim_expires_at=(
                now + timedelta(seconds=_DISPATCH_CLAIM_LEASE_SECONDS) if admitted else None
            ),
            dispatch_retry_at=None,
            idempotency_key=idempotency_key,
            # Informational provenance only — which beat instance dispatched
            # this run — not an active connector lease (lease_expires_at stays
            # unset; the source-level lease is untouched here).
            lease_owner=lease_owner,
            fencing_token=0,
            retry_count=0,
        )
        db.add(run)
    elif run.status not in ("queued", "running"):
        # A terminal run proves this occurrence was already consumed. This
        # handles a stale due row without publishing a second task.
        schedule.next_due_at = _next_fire_utc(schedule.cron_expression, tz, due_at)
        db.commit()
        return None
    elif run.dispatch_state == "dispatched":
        # The publish completed but schedule advancement was not observed by
        # this invocation; advance now without duplicating the broker send.
        schedule.next_due_at = _next_fire_utc(schedule.cron_expression, tz, due_at)
        db.commit()
        return None
    elif not admitted:
        run.dispatch_state = "backpressured"
        run.dispatch_reason = _BACKPRESSURE_REASON
        run.dispatch_claim_owner = None
        run.dispatch_claim_expires_at = None
        schedule.last_dispatched_run_id = run.id
        schedule.next_due_at = due_at
        db.commit()
        return None
    else:
        # A retained backpressured/publish-failed/pending row is now admitted;
        # persist the claim before attempting broker publication.
        run.dispatch_state = "pending"
        run.dispatch_reason = None
        run.dispatch_claim_owner = lease_owner
        run.dispatch_claim_expires_at = now + timedelta(seconds=_DISPATCH_CLAIM_LEASE_SECONDS)
        run.dispatch_retry_at = None

    schedule.last_dispatched_run_id = run.id
    # Keep the occurrence due until publication succeeds. This is the durable
    # retry cursor for both backpressure and broker failures.
    schedule.next_due_at = due_at

    db.commit()
    db.refresh(run)

    if not admitted:
        return None

    message = build_refresh_dispatch_message(run_id=run.id, task_name=task_name)
    try:
        send_refresh(message)
    except Exception:
        run.dispatch_state = "publish_failed"
        run.retry_count += 1
        max_attempts, _ = _retry_settings(schedule)
        run.dispatch_claim_owner = None
        run.dispatch_claim_expires_at = None
        if run.retry_count >= max_attempts:
            run.dispatch_reason = _RETRY_EXHAUSTED_REASON
            run.dispatch_retry_at = None
        else:
            run.dispatch_reason = _PUBLISH_FAILED_REASON
            run.dispatch_retry_at = _retry_at(schedule, now=now, retry_count=run.retry_count)
        db.commit()
        return None

    run.dispatch_state = "dispatched"
    run.dispatch_reason = None
    run.dispatch_claim_owner = None
    run.dispatch_claim_expires_at = None
    run.dispatch_retry_at = None
    schedule.next_due_at = _next_fire_utc(schedule.cron_expression, tz, due_at)
    db.commit()
    return run.id


def dispatch_due_schedules(
    db: Session, *, now: datetime, lease_owner: str,
    send_refresh: Callable[[RefreshDispatchMessage], str],
) -> list[str]:
    """Dispatch every enabled schedule whose `next_due_at` is due, one
    occurrence each. Each schedule is locked and re-checked before
    dispatch, so a schedule concurrently claimed (or already advanced past
    `now`) by another beat instance is skipped rather than double-dispatched."""
    due_ids = db.execute(
        select(RefreshSchedule.id).where(
            RefreshSchedule.enabled.is_(True),
            RefreshSchedule.next_due_at.isnot(None),
            RefreshSchedule.next_due_at <= now,
        ).order_by(RefreshSchedule.next_due_at)
    ).scalars().all()

    dispatched: list[str] = []
    for schedule_id in due_ids:
        run_id = _dispatch_one_schedule(
            db, schedule_id=schedule_id, now=now, lease_owner=lease_owner, send_refresh=send_refresh,
        )
        if run_id is not None:
            dispatched.append(run_id)
    return dispatched
