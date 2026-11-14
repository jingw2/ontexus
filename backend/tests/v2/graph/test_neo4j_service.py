"""Neo4jService 단위 테스트 — 실제 Neo4j 없이 mock 사용"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock


def make_mock_driver():
    driver = MagicMock()
    driver.verify_connectivity.return_value = None
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_neo4j_service_available_on_connect():
    """Neo4j 연결 성공 시 available=True"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb, \
            patch("app.services.v2.graph.neo4j_service._port_open", return_value=True):
        mock_driver, _ = make_mock_driver()
        mock_gdb.driver.return_value = mock_driver

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://localhost:7687", user="neo4j", password="test")
        assert svc.available is True


def test_neo4j_service_unavailable_on_error():
    """Neo4j 연결 실패 시 available=False"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("Connection refused")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://bad:9999", user="x", password="x")
        assert svc.available is False


def test_upsert_entity_unavailable_returns_none():
    """Neo4j 미연결 시 upsert_entity는 None 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.upsert_entity("Entity", {"id": "e1"})
        assert result is None


def test_get_graph_data_unavailable_returns_empty():
    """Neo4j 미연결 시 빈 그래프 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.get_graph_data("ontology-1")
        assert result == {"nodes": [], "edges": []}


def test_run_cypher_unavailable_returns_empty():
    """Neo4j 미연결 시 빈 리스트 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.run_cypher("MATCH (n) RETURN n")
        assert result == []


def test_cypher_builder_label_validation():
    """유효하지 않은 레이블은 ValueError"""
    from app.services.v2.graph.cypher_builder import validate_label
    assert validate_label("Entity") == "Entity"
    assert validate_label("Supply_Chain") == "Supply_Chain"
    with pytest.raises(ValueError):
        validate_label("Bad Label")
    with pytest.raises(ValueError):
        validate_label("1BadLabel")
    with pytest.raises(ValueError):
        validate_label("'; DROP DATABASE")


def test_cypher_builder_build_match():
    from app.services.v2.graph.cypher_builder import build_match_by_id
    query, params = build_match_by_id("Entity", "e1")
    assert "MATCH" in query
    assert params["id"] == "e1"


def test_batch_upsert_unavailable_returns_zero():
    """Neo4j 미연결 시 batch_upsert_entities는 0 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        count = svc.batch_upsert_entities("Entity", [{"id": "e1"}])
        assert count == 0


def test_legacy_bridge_sync_unavailable_graceful():
    """Neo4j 미연결 시 bridge sync가 오류 없이 처리"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.legacy_extraction_bridge import LegacyExtractionBridge
        bridge = LegacyExtractionBridge()
        # 오류 없이 실행되어야 함
        bridge.sync_to_neo4j("ont-1", [{"id": "e1", "type": "Entity"}], [])
