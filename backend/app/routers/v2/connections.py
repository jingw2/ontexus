"""
v2 Connection 管理 API
POST   /api/v2/connections
GET    /api/v2/connections
GET    /api/v2/connections/{id}
POST   /api/v2/connections/{id}/test
DELETE /api/v2/connections/{id}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.v2.connection import Connection
from app.models.user import User
from app.services.connection.config import (
    ConnectionConfigError,
    build_connector_config,
    safe_connection_error,
)
from app.services.connection.registry import get_connector

router = APIRouter(dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Pydantic 模式 ─────────────────────────────────────────────

class ConnectionCreate(BaseModel):
    name: str
    kind: str  # file | mysql | postgres | mongo | rest
    config: dict  # 明文连接配置 (服务端加密)


class ConnectionResponse(BaseModel):
    id: str
    name: str
    kind: str
    status: str

    class Config:
        from_attributes = True


# ── 端点 ──────────────────────────────────────────────────────

@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(body: ConnectionCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """创建连接。config 加密后存储。"""
    from app.services import encryption_service
    encrypted_config = {"_encrypted": encryption_service.encrypt(json.dumps(body.config))}

    conn = Connection(
        name=body.name,
        kind=body.kind,
        config=encrypted_config,
        status="inactive",
        created_by=current_user.id,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.get("", response_model=list[ConnectionResponse])
def list_connections(db: Session = Depends(get_db)):
    return db.query(Connection).all()


@router.get("/{connection_id}", response_model=ConnectionResponse)
def get_connection(connection_id: str, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


class TestConfigBody(BaseModel):
    type: str
    config: dict = {}


def _build_db_config(raw_config: dict, db_type: str) -> dict:
    """
    ConnectorInspector 发送单个字段（host/port/user/password/database）而非
    connection_string，这里组装成 SQLAlchemy 可用的连接 URL。
    密码中的特殊字符通过 urllib.parse.quote 编码以避免 URL 解析歧义。
    """
    return build_connector_config(db_type, raw_config)


def _load_connection_connector(
    conn: Connection,
    *,
    resource: str | None = None,
):
    """Build a connector from an encrypted Connection row.

    Keeping this in one helper ensures API tests, pipeline execution, and
    refresh workers all resolve the same saved database configuration.
    """
    config = build_connector_config(conn.kind, conn.config, resource=resource)
    return get_connector(conn.kind, config)


@router.post("/test-config")
def test_connection_config(body: TestConfigBody, _=Depends(require_editor)):
    """测试连接配置（无需先创建 Connection，供 Builder 使用）"""
    try:
        cfg = build_connector_config(body.type, body.config)
        connector = get_connector(body.type, cfg)
        ok = connector.test_connection()
        result = {"success": ok}
        if not ok:
            result["detail"] = getattr(connector, "last_error", None) or "connection test failed"
        return result
    except Exception as e:
        return {"success": False, "detail": safe_connection_error(e)}


@router.post("/{connection_id}/test")
def test_connection(connection_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    """连接测试。尝试真实连接并返回结果。"""
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    try:
        connector = _load_connection_connector(conn)
        ok = connector.test_connection()
        conn.status = "active" if ok else "error"
        db.commit()
        result = {"success": ok, "status": conn.status}
        if not ok:
            result["detail"] = getattr(connector, "last_error", None) or "connection test failed"
        return result
    except Exception as e:
        conn.status = "error"
        db.commit()
        return {"success": False, "status": "error", "detail": safe_connection_error(e)}


@router.get("/{connection_id}/resources")
def list_connection_resources(
    connection_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_editor),
):
    """List tables/collections exposed by a saved connection."""
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, "Connection not found")
    try:
        connector = _load_connection_connector(conn)
        resources = connector.list_resources()
        return {"connection_id": connection_id, "resources": resources}
    except (ConnectionConfigError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Unable to list resources for connection '{conn.name}': {safe_connection_error(exc)}",
        ) from exc


@router.get("/{connection_id}/resources/{resource:path}/sample")
def sample_connection_resource(
    connection_id: str,
    resource: str,
    limit: int = Query(20, ge=1, le=1000),
    db: Session = Depends(get_db),
    _=Depends(require_editor),
):
    """Read a bounded sample from one saved connection resource."""
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, "Connection not found")
    try:
        connector = _load_connection_connector(conn, resource=resource)
        rows = connector.pull_sample(resource, limit=limit)
        return {"connection_id": connection_id, "resource": resource, "rows": rows}
    except (ConnectionConfigError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Unable to read resource '{resource}': {safe_connection_error(exc)}",
        ) from exc


class RefreshConfigurationUpdate(BaseModel):
    resource: str
    cursor_contract: str  # "watermark_primary_key" | "opaque_source_cursor"
    configuration: dict = {}


@router.put("/{connection_id}/refresh-configuration", response_model=ConnectionResponse)
def update_connection_refresh_configuration(
    connection_id: str, body: RefreshConfigurationUpdate,
    db: Session = Depends(get_db), _=Depends(require_editor),
):
    """管理侧的资源刷新配置修订：在同一数据库事务内更新 Connection 的
    cursor_contract 与该 (connection, resource) 的权威 RefreshSourceState
    修订号，二者必须原子生效——绝不允许 Connection 侧的 JSON/cursor_contract
    单独变更而不递增 config_version、失效旧的 lease/fence。
    """
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    from app.services.v2.incremental.contract import update_source_configuration

    conn.cursor_contract = body.cursor_contract
    # update_source_configuration is the sole commit boundary for this
    # request, so the Connection mutation above lands in the exact same
    # transaction as the RefreshSourceState revision bump below.
    update_source_configuration(
        db, source_id=connection_id, resource=body.resource,
        cursor_contract=body.cursor_contract, configuration=body.configuration,
        now=datetime.now(timezone.utc),
    )
    db.refresh(conn)
    return conn


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    db.delete(conn)
    db.commit()


class ScheduleUpdate(BaseModel):
    """Enterprise timing controls for a connection's persisted refresh
    schedule — mirrors `schedule_service.ScheduleRequest`'s optional fields
    so `cron_expr` alone keeps behaving exactly as before."""
    cron_expr: str
    timezone: str = "UTC"
    business_calendar: list[str] = []
    sla_seconds: int = 0
    retry_policy: Optional[dict] = None
    backfill_window_seconds: int = 0
    max_pending_runs: int = 1
    enabled: bool = True


@router.post("/{connection_id}/schedule")
def set_schedule(connection_id: str, body: ScheduleUpdate, db: Session = Depends(get_db), _=Depends(require_editor)):
    """Compatibility delegate for the typed refresh schedule operation."""
    from app.services.v2.incremental.operations import RefreshError, update_refresh_schedule
    from app.services.v2.scheduler.cron_service import CronService

    try:
        schedule = update_refresh_schedule(
            db, source_id=connection_id, cron_expr=body.cron_expr,
            timezone_name=body.timezone, business_calendar=body.business_calendar,
            sla_seconds=body.sla_seconds, retry_policy=body.retry_policy,
            backfill_window_seconds=body.backfill_window_seconds,
            max_pending_runs=body.max_pending_runs, enabled=body.enabled,
        )
    except RefreshError as exc:
        if exc.reason_code == "SOURCE_NOT_FOUND":
            raise HTTPException(404, "Connection not found") from exc
        raise HTTPException(422, detail=exc.reason_code) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {
        "connection_id": connection_id,
        "cron": body.cron_expr,
        "celery_crontab": CronService().parse_cron(body.cron_expr),
        "status": "scheduled",
        "next_due_at": schedule.next_due_at.isoformat() if schedule.next_due_at else None,
    }


@router.post("/{connection_id}/sync")
def trigger_sync(connection_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    """手动触发数据同步 through the persisted refresh policy.

    The compatibility response/task name is retained for valid batch and
    micro-batch connections, but event-driven sources must use the signed
    webhook route and can never be sent to the polling queue.
    """
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, "Connection not found")

    from app.services.v2.incremental.operations import RefreshError
    from app.tasks.v2.refresh_tasks import create_manual_connection_run, refresh_connection_task
    try:
        run = create_manual_connection_run(db, connection_id)
    except RefreshError as exc:
        raise HTTPException(status_code=422, detail=exc.reason_code) from exc

    conn.status = "active"
    db.commit()

    try:
        refresh_connection_task.delay(run.id)
    except Exception as e:
        return {"connection_id": connection_id, "run_id": run.id, "status": "sync_failed",
                "error": f"任务派发失败 (Celery/Redis 不可用?): {e}"}

    return {"connection_id": connection_id, "run_id": run.id, "status": "sync_triggered"}
