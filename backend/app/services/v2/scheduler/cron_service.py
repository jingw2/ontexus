"""Cron 调度服务 — 验证、解析、注册定时任务"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 5段式 cron 表达式格式验证
CRON_PATTERN = re.compile(
    r'^(\*|[0-9,\-\*/]+)\s+'
    r'(\*|[0-9,\-\*/]+)\s+'
    r'(\*|[0-9,\-\*/]+)\s+'
    r'(\*|[0-9,\-\*/]+)\s+'
    r'(\*|[0-9,\-\*/]+)$'
)


class CronService:
    """管理 Connection 和 Pipeline 的定时同步/运行任务"""

    def validate_cron(self, expression: str) -> bool:
        """验证 cron 表达式格式（5段式）"""
        return bool(CRON_PATTERN.match(expression.strip()))

    def parse_cron(self, expression: str) -> dict:
        """将 cron 表达式解析为 Celery crontab 参数字典"""
        if not self.validate_cron(expression):
            raise ValueError(f"无效的 cron 表达式：{expression}")
        parts = expression.strip().split()
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day_of_month": parts[2],
            "month_of_year": parts[3],
            "day_of_week": parts[4],
        }

    def schedule_connection_sync(self, connection_id: str, cron_expr: str, *, db: "Session | None" = None,
                                  **schedule_kwargs) -> dict:
        """为 Connection 注册定时同步任务。

        仅在提供 `db` 会话时才会持久化 `RefreshSchedule` 行并计算 next_due_at,
        返回 status="scheduled"；未提供 db 时仅完成 cron 语法校验，返回
        status="validated" —— 校验本身绝不能被上报为"已生效调度"
        (Task 7 Deliverable)。
        """
        cron_params = self.parse_cron(cron_expr)
        if db is None:
            logger.info(f"Connection {connection_id} cron 校验通过（未持久化）: {cron_expr}")
            return {
                "connection_id": connection_id,
                "cron": cron_expr,
                "celery_crontab": cron_params,
                "status": "validated",
            }

        schedule = self._persist_schedule(db, target_type="connection", target_id=connection_id,
                                          cron_expr=cron_expr, **schedule_kwargs)
        logger.info(f"Connection {connection_id} 调度已持久化: {cron_expr}")
        return {
            "connection_id": connection_id,
            "cron": cron_expr,
            "celery_crontab": cron_params,
            "status": "scheduled",
            "next_due_at": schedule.next_due_at.isoformat() if schedule.next_due_at else None,
        }

    def schedule_pipeline_run(self, pipeline_id: str, cron_expr: str, *, db: "Session | None" = None,
                               **schedule_kwargs) -> dict:
        """Reject unsupported pipeline-target refresh schedules."""
        raise ValueError(
            "PIPELINE_SCHEDULE_UNSUPPORTED: pipeline-target schedules are not supported"
        )

    @staticmethod
    def _persist_schedule(db: "Session", *, target_type: str, target_id: str, cron_expr: str,
                          timezone_name: str = "UTC", business_calendar: list | None = None,
                          sla_seconds: int = 0, retry_policy: dict | None = None,
                          backfill_window_seconds: int = 0, max_pending_runs: int = 1,
                          enabled: bool = True):
        from app.services.v2.scheduler.schedule_service import ScheduleRequest, upsert_refresh_schedule

        request = ScheduleRequest(
            target_type=target_type, target_id=target_id, cron_expr=cron_expr, timezone=timezone_name,
            business_calendar=business_calendar or [], sla_seconds=sla_seconds, retry_policy=retry_policy,
            backfill_window_seconds=backfill_window_seconds, max_pending_runs=max_pending_runs, enabled=enabled,
        )
        return upsert_refresh_schedule(db, request, now=datetime.now(timezone.utc))

    def describe_cron(self, expression: str) -> str:
        """将 cron 表达式转换为人类可读描述"""
        if not self.validate_cron(expression):
            return "无效的 cron 表达式"
        parts = expression.strip().split()
        minute, hour, dom, month, dow = parts

        if expression.strip() == "* * * * *":
            return "每分钟"
        if minute.startswith("*/"):
            n = minute[2:]
            return f"每 {n} 分钟"
        if minute == "0" and dom == "*" and month == "*" and dow == "*":
            if hour == "*":
                return "每小时整点"
            if hour == "8":
                return "每天 08:00"
            return f"每天 {hour}:00"
        return f"按计划执行 ({expression.strip()})"
