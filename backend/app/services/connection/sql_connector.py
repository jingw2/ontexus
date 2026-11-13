"""关系型数据库 Connector — MySQL / PostgreSQL"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, inspect, text

from app.services.connection.base import ConnectorBase, DeltaPage, records_to_delta_page
from app.services.connection.config import safe_connection_error
from app.schemas.refresh import SourceCursor

# 合法 SQL 标识符：字母、数字、下划线、点（schema.table）；禁止空格/分号/引号/注释等
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_LEGACY_UNSET = object()


def _validate_identifier(name: str, field: str = "resource") -> str:
    """校验表名/列名等 SQL 标识符，防止 SQL 注入。"""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid {field}: {name!r}")
    return name


class SQLConnector(ConnectorBase):
    """
    基于 SQLAlchemy 的关系型数据库 Connector。
    config 示例:
      {
        "connection_string": "postgresql://user:pass@host:5432/db",
        "query": "SELECT * FROM orders",
        "watermark_column": "updated_at"   # APPEND 模式使用
      }
    """

    def __init__(self, config: dict):
        self._config = config
        self._engine = None
        self.last_error: str | None = None

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_engine(
                self._config["connection_string"],
                pool_pre_ping=True,
                connect_args={"connect_timeout": 10},
            )
        return self._engine

    def test_connection(self) -> bool:
        try:
            with self._get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = safe_connection_error(exc)
            return False

    def list_resources(self) -> list[str]:
        """返回数据库中的表列表"""
        inspector = inspect(self._get_engine())
        return inspector.get_table_names()

    def pull_sample(self, resource: str, limit: int = 100) -> list[dict]:
        """从表中查询样本数据"""
        _validate_identifier(resource)
        with self._get_engine().connect() as conn:
            result = conn.execute(
                text(f"SELECT * FROM {resource} LIMIT :limit"),
                {"limit": limit},
            )
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result]

    def pull_full(self, resource: str) -> list[dict]:
        """查询表全量数据"""
        import pandas as pd
        _validate_identifier(resource)
        query = self._config.get("query") or f"SELECT * FROM {resource}"
        return pd.read_sql(query, self._get_engine()).to_dict(orient="records")

    def pull_delta(
        self,
        resource: str,
        *,
        cursor: SourceCursor | None = None,
        overlap_window: timedelta = timedelta(0),
        since: str | None | object = _LEGACY_UNSET,
    ) -> DeltaPage | list[dict]:
        """Cursor-aware incremental query using a server-owned tuple key.

        ``since`` is retained only for old callers.  New callers receive a
        ``DeltaPage`` and never trigger a full-read fallback after a source
        error.
        """
        _validate_identifier(resource)
        watermark_col = self._config.get("watermark_column")
        primary_key_col = self._config.get("primary_key_column")

        # Compatibility path for the pre-Task-8 API.
        if since is not _LEGACY_UNSET:
            if not watermark_col or not since:
                return self.pull_full(resource)
            _validate_identifier(watermark_col, field="watermark_column")
            if primary_key_col:
                _validate_identifier(primary_key_col, field="primary_key_column")
            base_query = self._config.get("query") or f"SELECT * FROM {resource}"
            delta_query = f"""
                SELECT * FROM ({base_query}) _t
                WHERE {watermark_col} > :since
            """
            with self._get_engine().connect() as conn:
                result = conn.execute(text(delta_query), {"since": since})
                cols = list(result.keys())
                return [dict(zip(cols, row)) for row in result]

        contract = self._config.get("cursor_contract", "watermark_primary_key")
        if contract != "watermark_primary_key" or not watermark_col or not primary_key_col:
            rows = self.pull_full(resource)
            if not isinstance(rows, list):
                rows = list(rows)
            return records_to_delta_page(
                rows, source_id=self._config.get("source_id", ""), resource=resource,
                contract=contract, cursor=cursor,
                primary_key_field=str(primary_key_col or "id"),
                watermark_field=str(watermark_col or "updated_at"),
                cursor_outcome="unchanged",
            )

        _validate_identifier(watermark_col, field="watermark_column")
        _validate_identifier(primary_key_col, field="primary_key_column")
        base_query = self._config.get("query") or f"SELECT * FROM {resource}"
        params: dict[str, object] = {}
        predicate = "1 = 1"
        if cursor is not None and cursor.watermark is not None:
            if overlap_window > timedelta(0):
                overlap_start: object = _subtract_overlap(cursor.watermark, overlap_window)
                params["overlap_start"] = overlap_start
                predicate = f"{watermark_col} >= :overlap_start"
            else:
                params["cursor_watermark"] = cursor.watermark
                if cursor.primary_key is None:
                    predicate = f"{watermark_col} > :cursor_watermark"
                else:
                    params["cursor_primary_key"] = cursor.primary_key
                    predicate = (
                        f"({watermark_col} > :cursor_watermark OR "
                        f"({watermark_col} = :cursor_watermark AND {primary_key_col} > :cursor_primary_key))"
                    )
        delta_query = f"""
            SELECT * FROM ({base_query}) _t
            WHERE {predicate}
            ORDER BY {watermark_col}, {primary_key_col}
        """
        with self._get_engine().connect() as conn:
            result = conn.execute(text(delta_query), params)
            cols = list(result.keys())
            rows = [dict(zip(cols, row)) for row in result]
        return records_to_delta_page(
            rows, source_id=self._config.get("source_id", ""), resource=resource,
            contract=contract, cursor=cursor,
            primary_key_field=primary_key_col, watermark_field=watermark_col,
            observed_at=datetime.now(timezone.utc),
        )


def _subtract_overlap(value: object, overlap_window: timedelta) -> object:
    if isinstance(value, datetime):
        return value - overlap_window
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return parsed - overlap_window
        except ValueError:
            return value
    return value
