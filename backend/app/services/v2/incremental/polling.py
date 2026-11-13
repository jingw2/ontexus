"""Durable semi-real-time polling for Task 8.

The service in this module is deliberately the small coordination layer
between a connector and the durable refresh contract from Task 6. Source
calls happen outside the outcome transaction; DatasetVersion, PipelineRun
lineage, and the cursor are committed only by ``record_refresh_outcome``.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.v2.connection import Connection
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.pipeline import Pipeline, PipelineRun, PipelineRunInput
from app.models.v2.refresh import (
    RefreshDeadLetter,
    RefreshInboxEvent,
    RefreshRun,
    RefreshRunTransition,
    RefreshSourceState,
)
from app.schemas.refresh import (
    CURSOR_CONTRACTS,
    ChangeEnvelope,
    ConfigurationDriftError,
    RefreshCancellationRequested,
    RefreshError,
    RefreshFencingError,
    RefreshLeaseError,
    RefreshPolicy,
    SourceCursor,
    cursor_order,
    dedupe_key,
)
from app.services.connection.base import DeltaPage, records_to_delta_page
from app.services.connection.registry import get_connector
from app.services.connection.config import build_connector_config, decrypt_connection_config
from app.services.v2.incremental.contract import (
    _cursor_to_json,
    _lock_or_create_source_state,
    assert_refresh_not_cancelled,
    claim_refresh_run,
    finalize_refresh_cancellation,
    mark_refresh_retryable,
    record_refresh_outcome,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshRunResult:
    """Stable, non-secret summary returned by a polling attempt."""

    run_id: str
    status: str
    config_version: int
    cursor_contract: str
    input_dataset_version_ids: list[str] = field(default_factory=list)
    pipeline_run_id: str | None = None
    cursor_before: SourceCursor | None = None
    cursor_after: SourceCursor | None = None
    duplicate_count: int = 0
    late_event_count: int = 0
    retry_count: int = 0
    dead_letter_count: int = 0
    source_lag_seconds: float | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    dispatch_state: str | None = None
    cancel_requested_at: datetime | None = None
    error_code: str | None = None

    @property
    def frozen_config_version(self) -> int:
        return self.config_version

    @property
    def late_count(self) -> int:
        return self.late_event_count

    @property
    def dlq_count(self) -> int:
        """Compatibility alias for callers that name the metric DLQ count."""
        return self.dead_letter_count

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "config_version": self.config_version,
            "cursor_contract": self.cursor_contract,
            "input_dataset_version_ids": list(self.input_dataset_version_ids),
            "pipeline_run_id": self.pipeline_run_id,
            "cursor_before": _cursor_to_json(self.cursor_before),
            "cursor_after": _cursor_to_json(self.cursor_after),
            "duplicate_count": self.duplicate_count,
            "late_event_count": self.late_event_count,
            "retry_count": self.retry_count,
            "dead_letter_count": self.dead_letter_count,
            "dlq_count": self.dead_letter_count,
            "source_lag_seconds": self.source_lag_seconds,
            "provenance": dict(self.provenance),
            "dispatch_state": self.dispatch_state,
            "cancel_requested_at": self.cancel_requested_at.isoformat() if self.cancel_requested_at else None,
            "error_code": self.error_code,
        }


def _connector_for_source(
    db: Session, *, source_id: str, resource: str,
    state: RefreshSourceState, connection: Connection | None,
):
    """Resolve a connector from durable source state (replaceable in tests)."""
    if connection is None:
        raise RefreshError("SOURCE_NOT_FOUND", f"no source connection {source_id}")
    config = _source_configuration(state, connection)
    config.setdefault("source_id", source_id)
    config.setdefault("resource", resource)
    return get_connector(connection.kind, config)


def _source_configuration(state: RefreshSourceState | None, connection: Connection | None) -> dict:
    raw = decrypt_connection_config(connection.config if connection is not None else {})
    config = dict(raw)
    if state is not None and state.configuration:
        # Refresh revisions carry source-owned cursor/policy settings while
        # credentials and connection details remain on Connection.
        config.update(state.configuration)
    resource = state.resource if state is not None else config.get("resource")
    if connection is None:
        return config
    return build_connector_config(connection.kind, config, resource=resource)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cursor_key(cursor: SourceCursor | None) -> tuple:
    if cursor is None:
        return (None, None)
    if cursor.contract == "opaque_source_cursor":
        return (cursor.opaque_value,)
    return (cursor.watermark, cursor.primary_key)


def _safe_cursor_compare(left: SourceCursor | None, right: SourceCursor | None) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return -1
    if right is None:
        return 1
    try:
        return cursor_order(left, right)
    except (TypeError, ValueError):
        left_key = tuple("" if part is None else str(part) for part in _cursor_key(left))
        right_key = tuple("" if part is None else str(part) for part in _cursor_key(right))
        return (left_key > right_key) - (left_key < right_key)


def _event_cursor(envelope: ChangeEnvelope, contract: str) -> SourceCursor | None:
    if contract == "opaque_source_cursor" and envelope.source_cursor is not None:
        return SourceCursor(
            source_id=envelope.source_id, resource=envelope.resource, contract=contract,
            watermark=None, primary_key=None, opaque_value=envelope.source_cursor,
            observed_at=_as_utc(envelope.occurred_at),
        )
    if contract == "watermark_primary_key" and envelope.watermark is not None:
        return SourceCursor(
            source_id=envelope.source_id, resource=envelope.resource, contract=contract,
            watermark=str(envelope.watermark), primary_key=str(envelope.primary_key),
            opaque_value=None, observed_at=_as_utc(envelope.occurred_at),
        )
    return None


def _normalize_page(page: DeltaPage | Sequence[Mapping[str, object]], *, source_id: str,
                    resource: str, contract: str, cursor: SourceCursor | None) -> DeltaPage:
    if isinstance(page, DeltaPage):
        return page
    # A legacy connector returning rows is treated as an explicit full batch.
    return records_to_delta_page(
        list(page), source_id=source_id, resource=resource, contract=contract,
        cursor=cursor, cursor_outcome="unchanged",
    )


def _resolve_dataset(db: Session, *, source_id: str, resource: str, connection: Connection | None) -> Dataset:
    dataset = db.execute(select(Dataset).where(Dataset.id == resource)).scalar_one_or_none()
    if dataset is None:
        dataset = db.execute(
            select(Dataset).where(
                Dataset.source_connection_id == source_id,
                Dataset.name == resource,
            )
        ).scalar_one_or_none()
    if dataset is None:
        dataset = Dataset(
            id=str(uuid.uuid4()), name=resource, source_connection_id=source_id, kind="structured",
        )
        db.add(dataset)
        db.flush()
    return dataset


def _resolve_pipeline(db: Session, *, dataset: Dataset) -> Pipeline:
    pipeline = db.execute(
        select(Pipeline).where(
            Pipeline.source_dataset_id == dataset.id,
            Pipeline.status != "disabled",
        ).order_by(Pipeline.created_at)
    ).scalars().first()
    if pipeline is not None:
        return pipeline
    pipeline = Pipeline(
        id=str(uuid.uuid4()), name=f"{dataset.name} refresh", source_dataset_id=dataset.id,
        spec={}, status="active",
    )
    db.add(pipeline)
    db.flush()
    return pipeline


def _serialize_envelopes(envelopes: Sequence[ChangeEnvelope]) -> bytes:
    rows = []
    for envelope in envelopes:
        rows.append({
            **dict(envelope.payload),
            "_event_id": envelope.event_id,
            "_operation": envelope.operation,
            "_primary_key": envelope.primary_key,
            "_watermark": envelope.watermark,
            "_source_cursor": envelope.source_cursor,
        })
    return json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")


def _max_cursor(before: SourceCursor | None, candidate: SourceCursor | None) -> SourceCursor | None:
    if candidate is None:
        return before
    if candidate.contract == "watermark_primary_key" and not candidate.watermark:
        return before
    if candidate.contract == "opaque_source_cursor" and candidate.opaque_value in (None, ""):
        return before
    if before is None:
        return candidate
    return candidate if _safe_cursor_compare(candidate, before) > 0 else before


def _envelope_sort_key(envelope: ChangeEnvelope, contract: str) -> tuple[str, str]:
    """Stable source order for deterministic materialization within a page."""
    if contract == "opaque_source_cursor":
        return ("" if envelope.source_cursor is None else str(envelope.source_cursor), envelope.primary_key)
    return ("" if envelope.watermark is None else str(envelope.watermark), envelope.primary_key)


def _deduplicate_persisted_events(
    db: Session, *, source_id: str, resource: str,
    envelopes: Sequence[ChangeEnvelope], now: datetime,
) -> tuple[list[ChangeEnvelope], int]:
    """Exclude event identities already materialized by an earlier poll.

    The source lease serializes polls for one source/resource. Locking the
    durable inbox row additionally makes the event identity explicit and
    lets a failed/cancelled outcome roll back any newly staged identity.
    ``received``/``dead_lettered`` rows remain eligible for this poll; only
    identities already marked ``processed`` or ``duplicate`` are excluded.
    """
    materialized: list[ChangeEnvelope] = []
    duplicate_count = 0
    for envelope in envelopes:
        row = db.execute(
            select(RefreshInboxEvent).where(
                RefreshInboxEvent.source_id == source_id,
                RefreshInboxEvent.resource == resource,
                RefreshInboxEvent.event_id == envelope.event_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if row is not None and row.state in {"processed", "duplicate"}:
            duplicate_count += 1
            continue

        event_hash = dedupe_key(envelope)
        if row is None:
            row = RefreshInboxEvent(
                id=str(uuid.uuid4()), source_id=source_id, resource=resource,
                event_id=envelope.event_id, event_hash=event_hash,
                state="received", received_at=envelope.received_at,
            )
            db.add(row)
        else:
            row.event_hash = event_hash
        row.state = "processed"
        row.processed_at = now
        materialized.append(envelope)
    db.flush()
    return materialized, duplicate_count


def _assert_configuration_current(db: Session, run: RefreshRun) -> None:
    """Prefer the typed revision error before lease/fence checks at safe points."""
    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == run.source_id,
            RefreshSourceState.resource == run.resource,
        )
    ).scalar_one_or_none()
    if state is None:
        raise RefreshError("SOURCE_STATE_MISSING", "no RefreshSourceState for this run")
    if run.config_version != state.config_version or run.cursor_contract != state.cursor_contract:
        raise ConfigurationDriftError("frozen config_version/cursor_contract no longer matches the source revision")


def _claim_queued_run(db: Session, *, run_id: str, lease_owner: str, now: datetime,
                      lease_seconds: int = 300) -> RefreshRun:
    """Claim a schedule-created queued run without creating a second run."""
    # populate_existing=True: same fix as contract.py's _lock_run (see that
    # function's comment) — without it, a RefreshRun already in this
    # session's identity map (e.g. loaded by db.get() just before this call)
    # is returned as-is, ignoring the row FOR UPDATE just locked and
    # re-read. Concretely: a cancellation committed by another process
    # between that load and this claim would be silently discarded, and a
    # terminally-cancelled run could be resurrected to "running".
    run = db.execute(
        select(RefreshRun).where(RefreshRun.id == run_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
    if run is None:
        db.rollback()
        raise RefreshError("REFRESH_RUN_NOT_FOUND", f"no RefreshRun {run_id}")
    if run.status in {"succeeded", "failed", "dead_lettered", "cancelled"}:
        db.commit()
        return run
    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == run.source_id,
            RefreshSourceState.resource == run.resource,
        ).with_for_update()
    ).scalar_one_or_none()
    if state is None:
        state = _lock_or_create_source_state(
            db,
            source_id=run.source_id,
            resource=run.resource,
            now=now,
            default_cursor_contract=run.cursor_contract,
        )
    if run.status == "cancel_requested":
        db.commit()
        return run
    if run.status == "running":
        lease_active = (
            state.lease_owner == run.lease_owner
            and state.fencing_token == run.fencing_token
            and _as_utc(state.lease_expires_at) is not None
            and _as_utc(state.lease_expires_at) > now
        )
        if lease_active:
            # Manual Task 7 runs already hold the lease; execute under that
            # durable owner instead of manufacturing a second claim.
            db.commit()
            db.refresh(run)
            return run
        if _as_utc(state.lease_expires_at) is not None and _as_utc(state.lease_expires_at) > now:
            raise RefreshLeaseError("REFRESH_LEASE_ACTIVE", "another refresh lease is active")
        # A redelivery after worker loss may reclaim the same durable run.
        # Advance the source fence so the old worker cannot publish an
        # outcome after this worker takes ownership.
        next_fence = state.fencing_token + 1
        expires = now + timedelta(seconds=lease_seconds)
        run.lease_owner = lease_owner
        run.lease_expires_at = expires
        run.fencing_token = next_fence
        state.fencing_token = next_fence
        state.lease_owner = lease_owner
        state.lease_expires_at = expires
        state.updated_at = now
        db.add(RefreshRunTransition(
            id=str(uuid.uuid4()), run_id=run.id, from_status="running",
            to_status="running", reason="LEASE_RECLAIMED", actor=lease_owner,
        ))
        db.commit()
        db.refresh(run)
        return run
    if _as_utc(state.lease_expires_at) is not None and _as_utc(state.lease_expires_at) > now:
        raise RefreshLeaseError("REFRESH_LEASE_ACTIVE", "another refresh lease is active")

    next_fence = state.fencing_token + 1
    expires = now + timedelta(seconds=lease_seconds)
    old_status = run.status
    run.status = "running"
    run.lease_owner = lease_owner
    run.lease_expires_at = expires
    run.fencing_token = next_fence
    if run.cursor_before_json is None:
        run.cursor_before_json = state.cursor_json
    state.fencing_token = next_fence
    state.lease_owner = lease_owner
    state.lease_expires_at = expires
    state.updated_at = now
    db.add(RefreshRunTransition(
        id=str(uuid.uuid4()), run_id=run.id, from_status=old_status,
        to_status="running", actor=lease_owner,
    ))
    db.commit()
    db.refresh(run)
    return run


def _release_source_lease(db: Session, run: RefreshRun, *, now: datetime) -> None:
    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == run.source_id,
            RefreshSourceState.resource == run.resource,
        ).with_for_update()
    ).scalar_one_or_none()
    if state is not None and state.fencing_token == run.fencing_token and state.lease_owner == run.lease_owner:
        state.lease_owner = None
        state.lease_expires_at = None
        state.updated_at = now
    db.commit()


def _dead_letter_run(db: Session, *, run_id: str, reason: str, now: datetime) -> RefreshRun:
    # populate_existing=True: same fix as contract.py's _lock_run (see that
    # function's comment) — without it, a RefreshRun already in this
    # session's identity map is returned as-is, ignoring the row FOR UPDATE
    # just locked and re-read. Concretely: an operator's cancellation
    # committed by another session between that load and this re-fetch would
    # be silently discarded.
    run = db.execute(
        select(RefreshRun).where(RefreshRun.id == run_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one()
    if run.status == "cancel_requested":
        # An operator's committed cancellation must win over a late
        # retry-exhaustion failure: finalize the same cancel_requested ->
        # cancelled transition this module's own cancellation branch uses
        # (contract.py's finalize_refresh_cancellation) instead of
        # overwriting it with dead_lettered and creating a
        # RefreshDeadLetter row an operator could replay, which would make a
        # cancelled run resurrectable.
        try:
            cancelled = finalize_refresh_cancellation(
                db, run_id=run.id, lease_owner=run.lease_owner, fencing_token=run.fencing_token, now=now,
            )
        except RefreshError:
            cancelled = db.get(RefreshRun, run.id)
        if cancelled.retry_reason != reason:
            cancelled.retry_reason = reason
            db.commit()
            db.refresh(cancelled)
        return cancelled
    if run.status in {"succeeded", "failed", "dead_lettered", "cancelled"}:
        db.commit()
        return run
    old_status = run.status
    run.status = "dead_lettered"
    run.retry_reason = reason
    run.terminal_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.dispatch_state = "pending"
    db.add(RefreshDeadLetter(
        id=str(uuid.uuid4()), source_id=run.source_id, resource=run.resource,
        run_id=run.id, reason=reason, delivery_attempts=run.retry_count,
    ))
    db.add(RefreshRunTransition(
        id=str(uuid.uuid4()), run_id=run.id, from_status=old_status,
        to_status="dead_lettered", reason=reason,
    ))
    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == run.source_id,
            RefreshSourceState.resource == run.resource,
        ).with_for_update()
    ).scalar_one_or_none()
    if state is not None and state.fencing_token == run.fencing_token:
        state.lease_owner = None
        state.lease_expires_at = None
        state.updated_at = now
    db.commit()
    db.refresh(run)
    return run


def _max_attempts(config: Mapping[str, object]) -> int:
    policy = config.get("retry_policy")
    raw = policy.get("max_attempts") if isinstance(policy, Mapping) else config.get("max_attempts")
    try:
        return max(1, min(20, int(raw))) if raw is not None else 3
    except (TypeError, ValueError):
        return 3


def _retry_backoff_seconds(config: Mapping[str, object], retry_count: int) -> int:
    policy = config.get("retry_policy")
    raw = policy.get("backoff_seconds", 0) if isinstance(policy, Mapping) else config.get("backoff_seconds", 0)
    try:
        base = max(0, min(3600, int(raw)))
    except (TypeError, ValueError):
        base = 0
    return min(3600, base * (2 ** max(0, retry_count - 1)))


def _result(db: Session, run: RefreshRun, *, source_lag_seconds: float | None = None,
            error_code: str | None = None) -> RefreshRunResult:
    ids = list(db.execute(
        select(PipelineRunInput.dataset_version_id)
        .where(PipelineRunInput.pipeline_run_id == run.pipeline_run_id)
        .order_by(PipelineRunInput.input_ordinal)
    ).scalars()) if run.pipeline_run_id else []
    cursor_before = run.cursor_before
    cursor_after = run.cursor_after or cursor_before
    dead_letter_count = db.query(RefreshDeadLetter).filter(RefreshDeadLetter.run_id == run.id).count()
    return RefreshRunResult(
        run_id=run.id,
        status=run.status,
        config_version=run.config_version,
        cursor_contract=run.cursor_contract,
        input_dataset_version_ids=ids,
        pipeline_run_id=run.pipeline_run_id,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        duplicate_count=run.duplicate_count or 0,
        late_event_count=run.late_count or 0,
        retry_count=run.retry_count or 0,
        dead_letter_count=dead_letter_count,
        source_lag_seconds=source_lag_seconds if source_lag_seconds is not None else run.lag_seconds,
        provenance=dict(run.source_provenance or {}),
        dispatch_state=run.dispatch_state,
        cancel_requested_at=run.cancel_requested_at,
        error_code=error_code,
    )


def poll_source(
    db: Session, *, source_id: str, resource: str, lease_owner: str, now: datetime,
    _run_id: str | None = None,
) -> RefreshRunResult:
    """Pull one source/resource and commit its durable outcome."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    connection = db.get(Connection, source_id)
    if connection is None:
        raise RefreshError("SOURCE_NOT_FOUND", f"no source connection {source_id}")
    if _run_id is not None:
        run = _claim_queued_run(db, run_id=_run_id, lease_owner=lease_owner, now=now)
        if run.status in {"succeeded", "failed", "dead_lettered", "cancelled"}:
            return _result(db, run)
        if run.status == "cancel_requested":
            try:
                cancelled = finalize_refresh_cancellation(
                    db, run_id=run.id, lease_owner=run.lease_owner or lease_owner,
                    fencing_token=run.fencing_token, now=now,
                )
            except RefreshError:
                cancelled = db.get(RefreshRun, run.id)
            return _result(db, cancelled)
    else:
        state = db.execute(
            select(RefreshSourceState).where(
                RefreshSourceState.source_id == source_id,
                RefreshSourceState.resource == resource,
            )
        ).scalar_one_or_none()
        source_config = _source_configuration(state, connection)
        policy_value = (
            (state.configuration or {}).get("refresh_policy")
            if state and state.configuration and state.configuration.get("refresh_policy") is not None
            else getattr(connection, "refresh_policy", None) or source_config.get("refresh_policy")
        )
        try:
            policy = RefreshPolicy(policy_value or RefreshPolicy.MICRO_BATCH.value)
        except ValueError:
            policy = RefreshPolicy.MICRO_BATCH
        default_contract = (
            state.cursor_contract if state is not None else None
        ) or getattr(connection, "cursor_contract", None) or source_config.get(
            "cursor_contract", "watermark_primary_key"
        )
        run = claim_refresh_run(
            db, source_id=source_id, resource=resource, policy=policy,
            idempotency_key=f"poll:{source_id}:{resource}:{now.isoformat()}:{uuid.uuid4()}",
            lease_owner=lease_owner, now=now,
            default_cursor_contract=default_contract,
        )

    state = db.execute(
        select(RefreshSourceState).where(
            RefreshSourceState.source_id == run.source_id,
            RefreshSourceState.resource == run.resource,
        )
    ).scalar_one_or_none()
    if state is None:
        raise RefreshError("SOURCE_STATE_MISSING", "no RefreshSourceState for this run")
    config = _source_configuration(state, connection)
    contract = run.cursor_contract if run.cursor_contract in CURSOR_CONTRACTS else "watermark_primary_key"
    overlap_seconds = config.get("overlap_window_seconds", config.get("overlap_seconds", 60))
    try:
        overlap_window = timedelta(seconds=max(0, int(overlap_seconds)))
    except (TypeError, ValueError):
        overlap_window = timedelta(seconds=60)

    before = run.cursor_before
    try:
        owner = run.lease_owner or lease_owner
        connector = _connector_for_source(
            db, source_id=run.source_id, resource=run.resource,
            state=state, connection=connection,
        )
        _assert_configuration_current(db, run)
        assert_refresh_not_cancelled(
            db, run_id=run.id, lease_owner=owner,
            fencing_token=run.fencing_token, now=now,
        )
        page = _normalize_page(
            connector.pull_delta(run.resource, cursor=before, overlap_window=overlap_window),
            source_id=run.source_id, resource=run.resource,
            contract=contract, cursor=before,
        )
        _assert_configuration_current(db, run)
        unique: list[ChangeEnvelope] = []
        seen: set[str] = set()
        duplicate_count = 0
        late_count = 0
        for envelope in page.envelopes:
            key = dedupe_key(envelope)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            unique.append(envelope)
            event_cursor = _event_cursor(envelope, contract)
            if before is not None and event_cursor is not None and _safe_cursor_compare(event_cursor, before) <= 0:
                late_count += 1
        unique, durable_duplicate_count = _deduplicate_persisted_events(
            db, source_id=run.source_id, resource=run.resource, envelopes=unique, now=now,
        )
        duplicate_count += durable_duplicate_count
        unique.sort(key=lambda envelope: _envelope_sort_key(envelope, contract))
        candidate = page.candidate_cursor if page.cursor_outcome != "unchanged" else before
        next_cursor = _max_cursor(before, candidate)
        cursor_outcome = (
            "advanced"
            if next_cursor is not None and _safe_cursor_compare(next_cursor, before) > 0
            else "unchanged"
        )
        run.duplicate_count = duplicate_count
        run.late_count = late_count
        run.lag_seconds = int(max(0, page.source_lag_seconds or 0))
        provenance = {
            "refresh_run_id": run.id,
            "source_id": run.source_id,
            "resource": run.resource,
            "config_version": run.config_version,
            "cursor_contract": run.cursor_contract,
            "cursor_outcome": cursor_outcome,
            "source_observed_at": page.source_observed_at.isoformat() if page.source_observed_at else None,
        }
        run.source_provenance = provenance
        db.flush()

        # Safe point immediately before tentative materialization. The final
        # fenced outcome check rolls back these uncommitted rows if cancel wins.
        _assert_configuration_current(db, run)
        assert_refresh_not_cancelled(
            db, run_id=run.id, lease_owner=owner,
            fencing_token=run.fencing_token, now=now,
        )
        dataset = _resolve_dataset(db, source_id=run.source_id, resource=run.resource, connection=connection)
        pipeline = _resolve_pipeline(db, dataset=dataset)
        from app.services.v2.dataset_service import DatasetService

        svc = DatasetService(db)
        version = svc.create_version(
            dataset.id, _serialize_envelopes(unique), rowcount=len(unique),
            refresh_run_id=run.id, source_cursor=next_cursor, observed_at=page.source_observed_at,
            commit=False,
        )
        pipeline_run = PipelineRun(
            id=str(uuid.uuid4()), pipeline_id=pipeline.id, status="success",
            started_at=now, finished_at=now, dataset_version_id=version.id,
            stats={
                "refresh_run_id": run.id, "rows_in": len(unique),
                "duplicate_count": duplicate_count, "late_event_count": late_count,
            },
        )
        db.add(pipeline_run)
        db.flush()
        completed = record_refresh_outcome(
            db, run_id=run.id, lease_owner=owner,
            fencing_token=run.fencing_token, config_version=run.config_version,
            cursor_contract=run.cursor_contract,
            input_dataset_version_ids=[version.id], pipeline_run_id=pipeline_run.id,
            next_cursor=next_cursor,
            quality_summary={"rows": len(unique), "duplicates": duplicate_count, "late": late_count},
            provenance=provenance, now=now,
            input_source_cursor=next_cursor,
            input_provenance=provenance,
        )
        return _result(db, completed, source_lag_seconds=page.source_lag_seconds)
    except RefreshCancellationRequested:
        db.rollback()
        current = db.get(RefreshRun, run.id)
        try:
            finalized = finalize_refresh_cancellation(
                db, run_id=run.id, lease_owner=current.lease_owner or lease_owner,
                fencing_token=current.fencing_token, now=now,
            )
        except RefreshError:
            finalized = db.get(RefreshRun, run.id)
        return _result(db, finalized)
    except ConfigurationDriftError:
        db.rollback()
        raise
    except SoftTimeLimitExceeded:
        db.rollback()
        raise
    except (RefreshFencingError, RefreshLeaseError):
        db.rollback()
        raise
    except Exception as exc:
        logger.exception("refresh poll failed for run %s", run.id)
        db.rollback()
        current = db.get(RefreshRun, run.id)
        max_attempts = _max_attempts(config)
        if current.retry_count + 1 >= max_attempts:
            current = _dead_letter_run(db, run_id=current.id, reason=str(exc), now=now)
            return _result(db, current, error_code="DEAD_LETTERED")
        current = mark_refresh_retryable(db, run_id=current.id, reason=str(exc), now=now)
        retry_delay = _retry_backoff_seconds(config, current.retry_count)
        current.dispatch_retry_at = now + timedelta(seconds=retry_delay) if retry_delay else None
        db.commit()
        db.refresh(current)
        _release_source_lease(db, current, now=now)
        return _result(db, current, error_code="RETRYABLE")


def replay_refresh(db: Session, *, run_id: str, now: datetime | None = None,
                   operator_id: str = "operator") -> RefreshRun:
    """Create a new idempotent run from a dead-lettered/failed run."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    old = db.get(RefreshRun, run_id)
    if old is None:
        raise RefreshError("REFRESH_RUN_NOT_FOUND", f"no RefreshRun {run_id}")
    if old.status not in {"failed", "dead_lettered"}:
        raise RefreshError("REPLAY_NOT_ALLOWED", "only failed or dead-lettered runs can be replayed")
    new = claim_refresh_run(
        db, source_id=old.source_id, resource=old.resource,
        policy=old.policy, idempotency_key=f"replay:{old.id}:{uuid.uuid4()}",
        lease_owner=f"{operator_id}:replay", now=now,
        default_cursor_contract=old.cursor_contract,
    )
    new.trigger = "replay"
    db.commit()
    db.refresh(new)
    for letter in db.query(RefreshDeadLetter).filter(
        RefreshDeadLetter.run_id == old.id,
        RefreshDeadLetter.replay_status == "pending",
    ).all():
        letter.replay_status = "replayed"
    db.commit()
    return new


replay_dead_letter = replay_refresh


def select_pinned_dataset_version(
    db: Session, *, dataset_id: str,
    input_dataset_version_ids: Sequence[str] | None = None,
) -> DatasetVersion | None:
    """Resolve one explicit version, then latest approved/latest durable."""
    if input_dataset_version_ids:
        version = db.execute(
            select(DatasetVersion).where(
                DatasetVersion.id == input_dataset_version_ids[0],
                DatasetVersion.dataset_id == dataset_id,
            )
        ).scalar_one_or_none()
        if version is None:
            raise RefreshError("DATASET_VERSION_NOT_FOUND", "pinned input version is not part of the dataset")
        return version
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None
    if dataset.latest_version_id:
        version = db.get(DatasetVersion, dataset.latest_version_id)
        if version is not None:
            return version
    return db.execute(
        select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id).order_by(DatasetVersion.version_no.desc())
    ).scalars().first()
