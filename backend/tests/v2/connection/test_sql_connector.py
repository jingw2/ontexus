"""SQLConnector 단위 테스트 — 실제 DB 없이 SQLAlchemy mock 사용"""
from datetime import timedelta
from unittest.mock import MagicMock, patch
import pytest

import app.services.connection.sql_connector as _sql_mod  # noqa: ensure module loaded


def test_sql_connector_test_connection_success():
    """test_connection이 SELECT 1 성공 시 True를 반환"""
    with patch.object(_sql_mod, "create_engine") as mock_engine_factory:
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine_factory.return_value = mock_engine

        from app.services.connection.sql_connector import SQLConnector
        connector = SQLConnector({"connection_string": "postgresql://test/test"})
        result = connector.test_connection()
        assert result is True


def test_sql_connector_test_connection_failure():
    """연결 실패 시 False 반환"""
    with patch.object(_sql_mod, "create_engine") as mock_engine_factory:
        mock_engine_factory.side_effect = Exception("Connection refused")
        from app.services.connection.sql_connector import SQLConnector
        connector = SQLConnector({"connection_string": "mysql://bad/db"})
        assert connector.test_connection() is False


def test_registry_mysql_returns_sql_connector():
    from app.services.connection.registry import CONNECTOR_REGISTRY
    from app.services.connection.sql_connector import SQLConnector
    assert CONNECTOR_REGISTRY["mysql"] is SQLConnector
    assert CONNECTOR_REGISTRY["postgres"] is SQLConnector


def test_saved_sql_connection_fields_are_compiled_to_a_safe_sqlalchemy_url():
    from app.services.connection.config import build_connector_config

    mysql = build_connector_config(
        "mysql",
        {
            "host": "127.0.0.1",
            "port": "3306",
            "database": "runtime",
            "user": "runtime",
            "password": "p@ss:word/with#chars",
        },
    )
    postgres = build_connector_config(
        "postgres",
        {
            "host": "127.0.0.1",
            "port": 5432,
            "database": "runtime",
            "user": "runtime",
            "password": "secret",
        },
    )

    assert mysql["connection_string"] == (
        "mysql+pymysql://runtime:p%40ss%3Aword%2Fwith%23chars@127.0.0.1:3306/runtime"
    )
    assert postgres["connection_string"] == "postgresql+psycopg2://runtime:secret@127.0.0.1:5432/runtime"


def test_sql_connector_keeps_actionable_failure_detail_without_exposing_password():
    from app.services.connection.sql_connector import SQLConnector

    connector = SQLConnector({
        "connection_string": "postgresql+psycopg2://user:secret@localhost:5432/runtime",
    })
    connector._get_engine = MagicMock(side_effect=RuntimeError("connection refused"))

    assert connector.test_connection() is False
    assert connector.last_error == "connection refused"
    assert "secret" not in connector.last_error


def test_get_connector_all_supported_kinds():
    from app.services.connection.registry import get_connector
    for kind in ["file", "mysql", "postgres"]:
        conn = get_connector(kind, {"connection_string": "x", "prefix": "/"})
        assert conn is not None


def test_sql_connector_pull_delta_no_watermark():
    """watermark_column 없으면 pull_full 호출"""
    from app.services.connection.sql_connector import SQLConnector
    connector = SQLConnector({"connection_string": "postgresql://test/test"})
    # pull_delta with no watermark_column should fall back to pull_full
    connector.pull_full = MagicMock(return_value=[{"id": 1}])
    result = connector.pull_delta("orders", since=None)
    connector.pull_full.assert_called_once_with("orders")
    assert result == [{"id": 1}]


def test_sql_connector_engine_lazy_init():
    """엔진은 첫 번째 _get_engine() 호출 시 생성된다"""
    from app.services.connection.sql_connector import SQLConnector
    connector = SQLConnector({"connection_string": "postgresql://test/test"})
    assert connector._engine is None  # 초기화 전


def test_sql_connector_list_resources_calls_inspect():
    """list_resources는 SQLAlchemy inspector를 통해 테이블 목록을 반환"""
    with patch.object(_sql_mod, "create_engine") as mock_engine_factory:
        mock_engine = MagicMock()
        mock_engine_factory.return_value = mock_engine

        with patch.object(_sql_mod, "inspect") as mock_inspect:
            mock_inspector = MagicMock()
            mock_inspector.get_table_names.return_value = ["users", "orders"]
            mock_inspect.return_value = mock_inspector

            from app.services.connection.sql_connector import SQLConnector
            connector = SQLConnector({"connection_string": "postgresql://test/test"})
            tables = connector.list_resources()
            assert tables == ["users", "orders"]
            mock_inspector.get_table_names.assert_called_once()


def test_sql_connector_delta_returns_page_with_tuple_overlap_predicate():
    from app.schemas.refresh import SourceCursor
    from app.services.connection.base import DeltaPage
    from app.services.connection.sql_connector import SQLConnector

    connector = SQLConnector({
        "connection_string": "postgresql://test/test",
        "source_id": "source-001",
        "cursor_contract": "watermark_primary_key",
        "watermark_column": "updated_at",
        "primary_key_column": "id",
    })
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.keys.return_value = ["id", "updated_at", "value"]
    mock_result.__iter__.return_value = iter([("101", "2026-08-26T01:00:00Z", "new")])
    mock_conn.execute.return_value = mock_result
    connector._engine = MagicMock()
    connector._engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    connector._engine.connect.return_value.__exit__ = MagicMock(return_value=False)

    page = connector.pull_delta(
        "orders",
        cursor=SourceCursor(
            source_id="source-001", resource="orders", contract="watermark_primary_key",
            watermark="2026-08-26T01:00:00Z", primary_key="100", opaque_value=None, observed_at=None,
        ),
        overlap_window=timedelta(minutes=5),
    )

    assert isinstance(page, DeltaPage)
    assert page.candidate_cursor.primary_key == "101"
    statement = str(mock_conn.execute.call_args.args[0])
    assert "updated_at >= :overlap_start" in statement
    assert "ORDER BY updated_at, id" in statement
    assert "overlap_start" in mock_conn.execute.call_args.args[1]


def test_base_connector_fallback_is_an_explicit_full_batch():
    from app.services.connection.base import ConnectorBase, DeltaPage

    class FullBatchConnector(ConnectorBase):
        def __init__(self):
            self._config = {
                "source_id": "source-001",
                "cursor_contract": "watermark_primary_key",
            }

        def test_connection(self):
            return True

        def list_resources(self):
            return ["orders"]

        def pull_sample(self, resource, limit=100):
            return []

        def pull_full(self, resource):
            return [{"id": "100", "updated_at": "2026-08-26T01:00:00Z"}]

    page = FullBatchConnector().pull_delta(
        "orders", cursor=None, overlap_window=timedelta(minutes=1),
    )

    assert isinstance(page, DeltaPage)
    assert page.cursor_outcome == "unchanged"
    assert len(page.envelopes) == 1
    assert page.candidate_cursor.watermark is None
