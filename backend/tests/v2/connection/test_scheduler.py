"""CronService 单元测试"""
from datetime import datetime, timezone

import pytest
from app.services.v2.scheduler.cron_service import CronService


def test_validate_cron_valid():
    svc = CronService()
    assert svc.validate_cron("* * * * *") is True
    assert svc.validate_cron("0 8 * * *") is True
    assert svc.validate_cron("*/5 * * * *") is True
    assert svc.validate_cron("0 0 * * 1-5") is True


def test_validate_cron_invalid():
    svc = CronService()
    assert svc.validate_cron("invalid") is False
    assert svc.validate_cron("* * *") is False
    assert svc.validate_cron("") is False


def test_parse_cron_returns_celery_params():
    svc = CronService()
    params = svc.parse_cron("0 8 * * *")
    assert params["minute"] == "0"
    assert params["hour"] == "8"
    assert params["day_of_month"] == "*"


def test_parse_cron_invalid_raises():
    svc = CronService()
    with pytest.raises(ValueError, match="无效"):
        svc.parse_cron("bad expression")


def test_schedule_connection_without_db_only_validates_never_reports_active():
    """Task 7 deliverable: validation alone never reports a schedule as
    active — without a db session, syntax validation happens but nothing is
    persisted, so status must not be "scheduled"."""
    svc = CronService()
    result = svc.schedule_connection_sync("conn-1", "0 8 * * *")
    assert result["status"] != "scheduled"
    assert result["connection_id"] == "conn-1"
    assert "celery_crontab" in result


def test_schedule_pipeline_without_db_only_validates_never_reports_active():
    svc = CronService()
    with pytest.raises(ValueError, match="PIPELINE_SCHEDULE_UNSUPPORTED"):
        svc.schedule_pipeline_run("pl-1", "*/30 * * * *")


def test_schedule_connection_with_db_persists_and_returns_scheduled(db):
    svc = CronService()
    result = svc.schedule_connection_sync("conn-1", "0 8 * * *", db=db)
    assert result["status"] == "scheduled"
    assert result["connection_id"] == "conn-1"
    assert result["next_due_at"] is not None

    from app.models.v2.refresh import RefreshSchedule
    row = db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "source", RefreshSchedule.target_id == "conn-1",
    ).first()
    assert row is not None
    assert row.next_due_at is not None


def test_schedule_pipeline_with_db_persists_and_returns_scheduled(db):
    svc = CronService()
    with pytest.raises(ValueError, match="PIPELINE_SCHEDULE_UNSUPPORTED"):
        svc.schedule_pipeline_run("pl-1", "*/30 * * * *", db=db)

    from app.models.v2.refresh import RefreshSchedule
    assert db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "pipeline", RefreshSchedule.target_id == "pl-1",
    ).count() == 0


def test_describe_cron_every_minute():
    svc = CronService()
    assert svc.describe_cron("* * * * *") == "每分钟"


def test_describe_cron_daily_8am():
    svc = CronService()
    assert svc.describe_cron("0 8 * * *") == "每天 08:00"


def test_describe_cron_invalid():
    svc = CronService()
    assert svc.describe_cron("bad") == "无效的 cron 表达式"


# ── Router-level tests (code review Task 7 findings 1-3) ────────────────────
#
# `/api/v2/connections/{id}/sync` and `/api/v2/connections/{id}/schedule` are
# defined in app.routers.v2.connections with a *local* `get_db` dependency
# (not app.deps.get_db), so the shared `client` fixture's override does not
# reach them — each test below overrides `connections_router.get_db` itself,
# matching the existing pattern in tests/v2/incremental/test_refresh_api.py.

NOW = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def _connections_db_override(db):
    from app.main import app
    from app.routers.v2 import connections as connections_router

    def override():
        yield db

    app.dependency_overrides[connections_router.get_db] = override
    yield
    app.dependency_overrides.pop(connections_router.get_db, None)


@pytest.fixture
def scheduler_operator_headers(db):
    from app.models.user import User
    from app.services.auth_service import create_access_token, hash_password

    user = User(
        id="scheduler-operator-001", username="scheduler-operator",
        email="scheduler-operator@test.com",
        password_hash=hash_password("operator123"), role="editor",
    )
    db.add(user)
    db.commit()
    token = create_access_token({"sub": user.id, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scheduler_connection(db):
    from app.models.v2.connection import Connection
    from app.models.v2.refresh import RefreshSourceState

    db.add(Connection(
        id="scheduler-conn-001", name="scheduler-source", kind="rest", status="active",
        config={"cursor_contract": "watermark_primary_key", "resource": "orders"},
        refresh_policy="micro_batch", cursor_contract="watermark_primary_key",
    ))
    db.add(RefreshSourceState(
        id="scheduler-state-001", source_id="scheduler-conn-001", resource="orders",
        cursor_contract="watermark_primary_key", config_version=1,
        cursor_json={
            "watermark": NOW.isoformat(), "primary_key": "1", "opaque_value": None,
            "observed_at": NOW.isoformat(),
        },
        configuration={"refresh_policy": "micro_batch"}, fencing_token=1,
    ))
    db.commit()


def test_repeated_sync_while_run_is_active_returns_clean_non_500(
    client, db, scheduler_connection, scheduler_operator_headers, _connections_db_override,
):
    """Finding 1: a fresh idempotency key is minted on every manual sync, so
    a second call while the first run's lease is still active must not
    surface claim_refresh_run's RefreshLeaseError as an unhandled 500."""
    first = client.post(
        "/api/v2/connections/scheduler-conn-001/sync", headers=scheduler_operator_headers,
    )
    second = client.post(
        "/api/v2/connections/scheduler-conn-001/sync", headers=scheduler_operator_headers,
    )

    assert first.status_code == 200
    assert first.json()["status"] == "sync_triggered"
    assert second.status_code < 500
    assert second.status_code in (409, 422)
    assert second.json()["detail"]


def test_set_schedule_persists_full_enterprise_timing_controls(
    client, db, scheduler_connection, scheduler_operator_headers, _connections_db_override,
):
    """Finding 2: timezone/business_calendar/sla_seconds/retry_policy/
    backfill_window_seconds/max_pending_runs must actually be settable
    through the connections router, not silently defaulted."""
    response = client.post(
        "/api/v2/connections/scheduler-conn-001/schedule",
        json={
            "cron_expr": "0 2 * * *",
            "timezone": "Asia/Shanghai",
            "business_calendar": ["2026-10-01"],
            "sla_seconds": 86400,
            "retry_policy": {"max_attempts": 5, "backoff_seconds": 30},
            "backfill_window_seconds": 172800,
            "max_pending_runs": 2,
            "enabled": True,
        },
        headers=scheduler_operator_headers,
    )

    assert response.status_code == 200

    from app.models.v2.refresh import RefreshSchedule

    row = db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "source",
        RefreshSchedule.target_id == "scheduler-conn-001",
    ).first()
    assert row is not None
    assert row.timezone == "Asia/Shanghai"
    assert row.excluded_dates == ["2026-10-01"]
    assert row.sla_seconds == 86400
    assert row.retry_policy == {"max_attempts": 5, "backoff_seconds": 30}
    assert row.backfill_window_seconds == 172800
    assert row.max_pending_runs == 2


def test_set_schedule_keeps_cron_only_backward_compatible_defaults(
    client, db, scheduler_connection, scheduler_operator_headers, _connections_db_override,
):
    """Finding 2 backward-compat requirement: a request with only cron_expr
    keeps working exactly as before, using the same defaults."""
    response = client.post(
        "/api/v2/connections/scheduler-conn-001/schedule",
        json={"cron_expr": "0 8 * * *"},
        headers=scheduler_operator_headers,
    )

    assert response.status_code == 200

    from app.models.v2.refresh import RefreshSchedule

    row = db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "source",
        RefreshSchedule.target_id == "scheduler-conn-001",
    ).first()
    assert row is not None
    assert row.timezone == "UTC"
    assert (row.excluded_dates or []) == []
    assert (row.sla_seconds or 0) == 0
    assert (row.backfill_window_seconds or 0) == 0
    assert row.max_pending_runs == 1
    assert row.enabled is True


def test_set_schedule_rejects_malformed_field_with_400_not_500(
    client, db, scheduler_connection, scheduler_operator_headers, _connections_db_override,
):
    """Finding 2/3(c): a malformed timing-control value (negative SLA) must
    surface as a 400, not propagate upsert_refresh_schedule's ValueError as
    an unhandled 500."""
    response = client.post(
        "/api/v2/connections/scheduler-conn-001/schedule",
        json={"cron_expr": "0 2 * * *", "sla_seconds": -1},
        headers=scheduler_operator_headers,
    )

    assert response.status_code == 400

    from app.models.v2.refresh import RefreshSchedule

    row = db.query(RefreshSchedule).filter(
        RefreshSchedule.target_type == "source",
        RefreshSchedule.target_id == "scheduler-conn-001",
    ).first()
    assert row is None


def test_set_schedule_rejects_malformed_cron_with_400_not_500(
    client, db, scheduler_connection, scheduler_operator_headers, _connections_db_override,
):
    response = client.post(
        "/api/v2/connections/scheduler-conn-001/schedule",
        json={"cron_expr": "not a cron"},
        headers=scheduler_operator_headers,
    )

    assert response.status_code == 400
