"""Graph connectivity diagnostics (graph-engineering playbook, Section V.E):
connected components, edge/node density, and hub nodes as a health signal
for whether entity resolution actually merged what it should have.
"""
from app.services.graph_diagnostics import compute_graph_diagnostics


def _entities(*names):
    return [{"id": n, "name_cn": n} for n in names]


def test_single_connected_component_when_fully_linked():
    entities = _entities("借款人", "贷款", "还款记录")
    edges = [("借款人", "贷款"), ("贷款", "还款记录")]
    diag = compute_graph_diagnostics(entities, edges)
    assert diag["connected_components"] == 1
    assert diag["largest_component_size"] == 3
    assert diag["isolated_entity_count"] == 0


def test_fragmented_islands_are_counted_separately():
    entities = _entities("A", "B", "C", "D")
    edges = [("A", "B"), ("C", "D")]
    diag = compute_graph_diagnostics(entities, edges)
    assert diag["connected_components"] == 2
    assert diag["largest_component_size"] == 2


def test_isolated_entities_are_zero_degree_and_reported():
    entities = _entities("A", "B", "孤立实体")
    edges = [("A", "B")]
    diag = compute_graph_diagnostics(entities, edges)
    assert diag["isolated_entity_count"] == 1
    assert diag["isolated_entities"] == ["孤立实体"]


def test_edge_node_ratio_and_hub_ranking():
    entities = _entities("hub", "a", "b", "c")
    edges = [("hub", "a"), ("hub", "b"), ("hub", "c")]
    diag = compute_graph_diagnostics(entities, edges)
    assert diag["entity_count"] == 4
    assert diag["edge_count"] == 3
    assert diag["edge_node_ratio"] == 0.75
    assert diag["hub_entities"][0] == {"name_cn": "hub", "degree": 3}


def test_dangling_edge_endpoints_are_ignored():
    """An edge referencing an entity id not in the ontology (a data quality
    issue caught elsewhere by P0 validation) must not crash diagnostics."""
    entities = _entities("A", "B")
    edges = [("A", "B"), ("A", "不存在的实体")]
    diag = compute_graph_diagnostics(entities, edges)
    assert diag["edge_count"] == 1
    assert diag["connected_components"] == 1


def test_empty_ontology_does_not_divide_by_zero():
    diag = compute_graph_diagnostics([], [])
    assert diag["entity_count"] == 0
    assert diag["edge_node_ratio"] == 0.0
    assert diag["connected_components"] == 0
