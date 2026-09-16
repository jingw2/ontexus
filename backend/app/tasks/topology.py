"""Celery execution topology (Task 6A) — named queues/routes, bounded
refresh worker settings, and the run-ID-only refresh dispatch boundary.

This is the single source of truth for how every Celery task in this
backend is routed to an isolated queue. `app.tasks.celery_app` calls
`configure_celery_topology` once at import time; workers and beat load only
that entry point (`-A app.tasks.celery_app`) — role/service names alone
never imply queue isolation, the `-Q` flag and this module's routes do.

The actual refresh task *bodies* (`refresh.connection`/`refresh.pipeline`/
`refresh.poll` in Task 7/8, `refresh.event` in Task 9, `refresh.replay` in
Task 10) are registered by those later tasks; this module only reserves
their names, queues, and bounded delivery settings so the contract is fixed
before any connector implementation lands.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from celery import Celery
from kombu import Queue

# ── Named queues ────────────────────────────────────────────────────────
QUEUE_REFRESH_SCHEDULE = "refresh.schedule"
QUEUE_REFRESH_POLL = "refresh.poll"
QUEUE_REFRESH_EVENT = "refresh.event"
QUEUE_REFRESH_REPLAY = "refresh.replay"
QUEUE_ARTIFACT_EXTRACTION = "artifact.extraction"
QUEUE_AGENT_INTERACTIVE = "agent.interactive"
QUEUE_HOUSEKEEPING = "housekeeping"

ALL_QUEUE_NAMES = (
    QUEUE_REFRESH_SCHEDULE,
    QUEUE_REFRESH_POLL,
    QUEUE_REFRESH_EVENT,
    QUEUE_REFRESH_REPLAY,
    QUEUE_ARTIFACT_EXTRACTION,
    QUEUE_AGENT_INTERACTIVE,
    QUEUE_HOUSEKEEPING,
)

# ── Refresh task names + full task routing table ───────────────────────
REFRESH_TASK_NAMES = (
    "refresh.dispatch_due_schedules",
    "refresh.connection",
    "refresh.pipeline",
    "refresh.poll",
    "refresh.event",
    "refresh.replay",
)

TASK_ROUTES: dict[str, dict[str, str]] = {
    "refresh.dispatch_due_schedules": {"queue": QUEUE_REFRESH_SCHEDULE},
    "refresh.connection": {"queue": QUEUE_REFRESH_POLL},
    "refresh.pipeline": {"queue": QUEUE_REFRESH_POLL},
    "refresh.poll": {"queue": QUEUE_REFRESH_POLL},
    "refresh.event": {"queue": QUEUE_REFRESH_EVENT},
    "refresh.replay": {"queue": QUEUE_REFRESH_REPLAY},
    "app.tasks.extraction.run_extraction": {"queue": QUEUE_ARTIFACT_EXTRACTION},
    "app.tasks.audit.run_audit": {"queue": QUEUE_ARTIFACT_EXTRACTION},
    "app.tasks.v2.mapping_apply.mapping_apply_task": {"queue": QUEUE_ARTIFACT_EXTRACTION},
    "app.tasks.v2.pipeline_run.pipeline_run_task": {"queue": QUEUE_ARTIFACT_EXTRACTION},
    "agent.turn_execute": {"queue": QUEUE_AGENT_INTERACTIVE},
    "agent.dispatch_claim": {"queue": QUEUE_AGENT_INTERACTIVE},
    "agent.dispatch_heartbeat": {"queue": QUEUE_AGENT_INTERACTIVE},
    "agent.interactive_probe": {"queue": QUEUE_AGENT_INTERACTIVE},
    "agent.dispatch_publish": {"queue": QUEUE_HOUSEKEEPING},
    "agent.dispatch_watchdog": {"queue": QUEUE_HOUSEKEEPING},
    "agent.dispatch_sweeper": {"queue": QUEUE_HOUSEKEEPING},
    "agent.index_consume": {"queue": QUEUE_HOUSEKEEPING},
    "agent.memory_summary_sweep": {"queue": QUEUE_HOUSEKEEPING},
    "agent.memory_extraction_sweep": {"queue": QUEUE_HOUSEKEEPING},
    "agent.memory_vector_sweep": {"queue": QUEUE_HOUSEKEEPING},
    "agent.retention_purge": {"queue": QUEUE_HOUSEKEEPING},
}

# These four share one queue/worker pool. Without acks_late + a time limit, a
# single hung LLM call (or a worker dying mid-task) permanently wedges one of
# `--concurrency` slots with nothing to free it — every later task on this
# queue then sits "queued" forever with no worker left to pick it up.
ARTIFACT_EXTRACTION_TASK_NAMES = (
    "app.tasks.extraction.run_extraction",
    "app.tasks.audit.run_audit",
    "app.tasks.v2.mapping_apply.mapping_apply_task",
    "app.tasks.v2.pipeline_run.pipeline_run_task",
)
ARTIFACT_EXTRACTION_SOFT_TIME_LIMIT_SECONDS = 1800
ARTIFACT_EXTRACTION_HARD_TIME_LIMIT_SECONDS = 1860


# ── Bounded refresh worker settings ─────────────────────────────────────
@dataclass(frozen=True)
class RefreshWorkerLimits:
    concurrency: int
    prefetch_multiplier: int
    soft_time_limit_seconds: int
    hard_time_limit_seconds: int
    shutdown_grace_seconds: int


_DEFAULT_LIMITS = RefreshWorkerLimits(
    concurrency=2,
    prefetch_multiplier=1,
    soft_time_limit_seconds=300,
    hard_time_limit_seconds=360,
    shutdown_grace_seconds=40,
)

_LIMIT_ENV_KEYS = {
    "concurrency": "REFRESH_WORKER_CONCURRENCY",
    "prefetch_multiplier": "REFRESH_WORKER_PREFETCH_MULTIPLIER",
    "soft_time_limit_seconds": "REFRESH_SOFT_TIME_LIMIT_SECONDS",
    "hard_time_limit_seconds": "REFRESH_HARD_TIME_LIMIT_SECONDS",
    "shutdown_grace_seconds": "REFRESH_WORKER_SHUTDOWN_GRACE_SECONDS",
}


def load_refresh_worker_limits(environ: Mapping[str, str]) -> RefreshWorkerLimits:
    """Load bounded, positive worker settings from the environment.

    A missing or empty value falls back to the local reference default
    (2/1/300/360/40). Rejects non-positive values and a soft time limit that
    is not strictly less than the hard time limit. These are operational
    defaults, not throughput claims.
    """
    values: dict[str, int] = {}
    for field_name, env_key in _LIMIT_ENV_KEYS.items():
        raw = environ.get(env_key)
        if raw is None or raw == "":
            values[field_name] = getattr(_DEFAULT_LIMITS, field_name)
            continue
        try:
            values[field_name] = int(raw)
        except ValueError as exc:
            raise ValueError(f"{env_key} must be an integer, got {raw!r}") from exc

    for field_name, value in values.items():
        if value <= 0:
            raise ValueError(f"{_LIMIT_ENV_KEYS[field_name]} must be positive, got {value}")
    if values["soft_time_limit_seconds"] >= values["hard_time_limit_seconds"]:
        raise ValueError(
            "REFRESH_SOFT_TIME_LIMIT_SECONDS must be less than REFRESH_HARD_TIME_LIMIT_SECONDS"
        )

    return RefreshWorkerLimits(**values)


def get_refresh_task_options(task_name: str) -> Mapping[str, object]:
    """Bounded delivery options for a refresh task — the same values
    `configure_celery_topology` installs as Celery task annotations,
    re-derived from the current environment's `RefreshWorkerLimits`."""
    if task_name not in REFRESH_TASK_NAMES:
        raise ValueError(f"not a refresh task: {task_name}")
    limits = load_refresh_worker_limits(os.environ)
    return {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "soft_time_limit": limits.soft_time_limit_seconds,
        "time_limit": limits.hard_time_limit_seconds,
    }


# ── Run-ID-only refresh dispatch ────────────────────────────────────────
@dataclass(frozen=True)
class RefreshDispatchMessage:
    run_id: str
    task_name: str
    queue: str

    def to_dict(self) -> dict[str, str]:
        return {"run_id": self.run_id, "task_name": self.task_name, "queue": self.queue}


def build_refresh_dispatch_message(*, run_id: str, task_name: str) -> RefreshDispatchMessage:
    """Build a dispatch message carrying only a durable `run_id`.

    Rejects an unknown (non-refresh) task name, an empty run ID, and — as a
    defensive invariant over this module's own routing table — a resolved
    queue that isn't one of the named queues. There is no parameter for a
    source URL, cursor, credential, SQL, selector, or event payload; passing
    one raises `TypeError` for the unexpected keyword argument.
    """
    if not run_id:
        raise ValueError("run_id is required")
    if task_name not in REFRESH_TASK_NAMES:
        raise ValueError(f"unknown refresh task name: {task_name}")
    queue = TASK_ROUTES[task_name]["queue"]
    if queue not in ALL_QUEUE_NAMES:
        raise ValueError(f"task/queue mismatch for {task_name}")
    return RefreshDispatchMessage(run_id=run_id, task_name=task_name, queue=queue)


def enqueue_refresh_run(
    *, message: RefreshDispatchMessage, send_task: Callable[[str, Sequence[str], str], str],
) -> str:
    """Hand a refresh run to the broker: the only argument ever sent is the
    durable `run_id`, on the run's routed queue. Returns the broker task ID."""
    # Keep oldest-age metadata separate from Celery's Redis list.  The
    # observer records only the durable run ID/timestamp and never reads the
    # task body.  Failure of this best-effort index must not alter dispatch.
    from app.services.v2.incremental.broker_observer import (
        record_refresh_dequeue,
        record_refresh_enqueue,
    )

    record_refresh_enqueue(queue=message.queue, run_id=message.run_id)
    try:
        return send_task(message.task_name, [message.run_id], message.queue)
    except Exception:
        record_refresh_dequeue(queue=message.queue, run_id=message.run_id)
        raise


# ── Celery configuration ────────────────────────────────────────────────
def configure_celery_topology(celery_app: Celery) -> None:
    """Install the named-queue/route contract onto `celery_app`.

    Sets `task_queues` for every named queue, `task_routes` for every named
    task, `task_default_queue=housekeeping`, worker prefetch of 1, task
    events (for operator metrics), and refresh-only annotations
    (`acks_late`, `reject_on_worker_lost`, bounded soft/hard time limits) so
    a refresh task is only ever acknowledged after a durable outcome and is
    safely redelivered on worker loss.
    """
    limits = load_refresh_worker_limits(os.environ)
    celery_app.conf.task_queues = tuple(Queue(name) for name in ALL_QUEUE_NAMES)
    celery_app.conf.task_routes = TASK_ROUTES
    celery_app.conf.task_default_queue = QUEUE_HOUSEKEEPING
    celery_app.conf.worker_prefetch_multiplier = 1
    celery_app.conf.worker_send_task_events = True
    celery_app.conf.task_send_sent_event = True
    celery_app.conf.task_annotations = {
        name: {
            "acks_late": True,
            "reject_on_worker_lost": True,
            "soft_time_limit": limits.soft_time_limit_seconds,
            "time_limit": limits.hard_time_limit_seconds,
        }
        for name in REFRESH_TASK_NAMES
    }
    celery_app.conf.task_annotations.update({
        name: {
            "acks_late": True,
            "reject_on_worker_lost": True,
            "soft_time_limit": ARTIFACT_EXTRACTION_SOFT_TIME_LIMIT_SECONDS,
            "time_limit": ARTIFACT_EXTRACTION_HARD_TIME_LIMIT_SECONDS,
        }
        for name in ARTIFACT_EXTRACTION_TASK_NAMES
    })
