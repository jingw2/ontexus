"""GraphAnalyticsService 单元测试（Neo4j Mock）"""
import pytest
from unittest.mock import patch, MagicMock
from app.services.v2.graph.graph_analytics import GraphAnalyticsService


def make_analytics(available=False):
    """创建使用 Mock Neo4j 的分析服务"""
    mock_neo4j = MagicMock()
    mock_neo4j.available = available
    return GraphAnalyticsService(neo4j=mock_neo4j), mock_neo4j


def test_get_neighbors_unavailable():
    svc, _ = make_analytics(available=False)
    result = svc.get_neighbors("ont-1", "node-1", depth=1)
    assert result["neo4j_available"] is False
    assert result["nodes"] == []


def test_shortest_path_unavailable():
    svc, _ = make_analytics(available=False)
    result = svc.shortest_path("ont-1", "src-1", "tgt-1")
    assert result["neo4j_available"] is False
    assert result["length"] == -1


def test_node_degree_unavailable():
    svc, _ = make_analytics(available=False)
    result = svc.node_degree("ont-1", "node-1")
    assert result["neo4j_available"] is False
    assert result["in_degree"] == 0


def test_top_connected_nodes_unavailable():
    svc, _ = make_analytics(available=False)
    result = svc.top_connected_nodes("ont-1")
    assert result == []


def test_shortest_path_no_result():
    svc, mock_neo4j = make_analytics(available=True)
    mock_neo4j.run_cypher.return_value = []
    result = svc.shortest_path("ont-1", "a", "b")
    assert result["length"] == -1
    assert "message" in result


def test_top_connected_nodes_with_result():
    svc, mock_neo4j = make_analytics(available=True)
    mock_neo4j.run_cypher.return_value = [
        {"node_id": "n1", "name": "Alice", "degree": 5},
        {"node_id": "n2", "name": "Bob",   "degree": 3},
    ]
    result = svc.top_connected_nodes("ont-1", limit=2)
    assert len(result) == 2
    assert result[0]["degree"] == 5


def test_node_degree_with_result():
    svc, mock_neo4j = make_analytics(available=True)
    mock_neo4j.run_cypher.return_value = [{"in_degree": 3, "out_degree": 7}]
    result = svc.node_degree("ont-1", "node-1")
    assert result["in_degree"] == 3
    assert result["out_degree"] == 7


def test_graph_traversals_scope_every_returned_node_to_ontology():
    svc, mock_neo4j = make_analytics(available=True)
    mock_neo4j.run_cypher.return_value = []

    svc.get_neighbors("ont-1", "node-1")
    neighbors_query = mock_neo4j.run_cypher.call_args.args[0]
    assert "all(node IN nodes(path) WHERE node.ontology_id = $ontology_id)" in neighbors_query

    svc.shortest_path("ont-1", "a", "b")
    path_query = mock_neo4j.run_cypher.call_args.args[0]
    assert "all(node IN nodes(p) WHERE node.ontology_id = $ontology_id)" in path_query

    svc.node_degree("ont-1", "node-1")
    degree_query = mock_neo4j.run_cypher.call_args.args[0]
    assert "out_neighbor.ontology_id = $ontology_id" in degree_query
    assert "in_neighbor.ontology_id = $ontology_id" in degree_query

    svc.top_connected_nodes("ont-1")
    top_query = mock_neo4j.run_cypher.call_args.args[0]
    assert "neighbor.ontology_id = $ontology_id" in top_query


def test_graph_neighbors_route_scopes_every_path_node(monkeypatch):
    from app.routers.v2 import graph

    fake_neo4j = MagicMock()
    fake_neo4j.available = True
    fake_neo4j.run_cypher.return_value = []
    monkeypatch.setattr(graph, "get_neo4j", lambda: fake_neo4j)

    graph.get_neighbors("ont-1", "node-1")

    query = fake_neo4j.run_cypher.call_args.args[0]
    assert "all(node IN nodes(path) WHERE node.ontology_id = $ontology_id)" in query
