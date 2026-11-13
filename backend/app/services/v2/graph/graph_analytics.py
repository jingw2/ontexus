"""高级图分析服务 — 邻居探索、最短路径、度统计"""
from __future__ import annotations
import logging
from app.services.v2.graph.neo4j_service import Neo4jService

logger = logging.getLogger(__name__)


class GraphAnalyticsService:
    """基于 Neo4j 的图分析操作"""

    def __init__(self, neo4j: Neo4jService | None = None):
        self._neo4j = neo4j or Neo4jService()

    def get_neighbors(self, ontology_id: str, node_id: str, depth: int = 1) -> dict:
        """获取节点的 N 度邻居"""
        if not self._neo4j.available:
            return {"nodes": [], "edges": [], "neo4j_available": False}
        depth = max(1, min(depth, 5))
        query = f"""
        MATCH path = (n)-[r*1..{depth}]-(m)
        WHERE elementId(n) = $node_id AND n.ontology_id = $ontology_id
          AND all(node IN nodes(path) WHERE node.ontology_id = $ontology_id)
        RETURN n, relationships(path) AS rels, nodes(path) AS all_nodes
        LIMIT 100
        """
        try:
            results = self._neo4j.run_cypher(query, {"node_id": node_id, "ontology_id": ontology_id})
            # 展平节点和边
            nodes_map = {}
            edges = []
            for record in results:
                for node in record.get("all_nodes", []):
                    if hasattr(node, "element_id"):
                        nid = node.element_id
                        if nid not in nodes_map:
                            nodes_map[nid] = {"id": nid, "labels": list(node.labels), "properties": dict(node)}
                for rel in record.get("rels", []):
                    if hasattr(rel, "element_id"):
                        edges.append({
                            "id": rel.element_id,
                            "type": rel.type,
                            "source": rel.start_node.element_id,
                            "target": rel.end_node.element_id,
                        })
            return {"nodes": list(nodes_map.values()), "edges": edges, "neo4j_available": True}
        except Exception as e:
            logger.warning(f"get_neighbors 失败: {e}")
            return {"nodes": [], "edges": [], "neo4j_available": True, "error": str(e)}

    def shortest_path(self, ontology_id: str, src_id: str, tgt_id: str) -> dict:
        """两节点间最短路径"""
        if not self._neo4j.available:
            return {"path": [], "length": -1, "neo4j_available": False}
        query = """
        MATCH (s), (t)
        WHERE elementId(s) = $src AND elementId(t) = $tgt
          AND s.ontology_id = $ontology_id AND t.ontology_id = $ontology_id
        MATCH p = shortestPath((s)-[*]-(t))
        WHERE all(node IN nodes(p) WHERE node.ontology_id = $ontology_id)
        RETURN [n IN nodes(p) | {id: elementId(n), labels: labels(n), name: n.name_cn}] AS path_nodes,
               length(p) AS path_length
        """
        try:
            results = self._neo4j.run_cypher(query, {"src": src_id, "tgt": tgt_id, "ontology_id": ontology_id})
            if results:
                r = results[0]
                return {
                    "path": r.get("path_nodes", []),
                    "length": r.get("path_length", -1),
                    "neo4j_available": True,
                }
            return {"path": [], "length": -1, "neo4j_available": True, "message": "两节点间无路径"}
        except Exception as e:
            logger.warning(f"shortest_path 失败: {e}")
            return {"path": [], "length": -1, "neo4j_available": True, "error": str(e)}

    def node_degree(self, ontology_id: str, node_id: str) -> dict:
        """查询节点的入度和出度"""
        if not self._neo4j.available:
            return {"in_degree": 0, "out_degree": 0, "neo4j_available": False}
        query = """
        MATCH (n)
        WHERE elementId(n) = $node_id AND n.ontology_id = $ontology_id
        OPTIONAL MATCH (n)-[out]->(out_neighbor)
        WHERE out_neighbor.ontology_id = $ontology_id
        OPTIONAL MATCH (in_neighbor)-[in]->(n)
        WHERE in_neighbor.ontology_id = $ontology_id
        RETURN count(DISTINCT out) AS out_degree, count(DISTINCT in) AS in_degree
        """
        try:
            results = self._neo4j.run_cypher(query, {"node_id": node_id, "ontology_id": ontology_id})
            if results:
                r = results[0]
                return {
                    "in_degree": r.get("in_degree", 0),
                    "out_degree": r.get("out_degree", 0),
                    "neo4j_available": True,
                }
        except Exception as e:
            logger.warning(f"node_degree 失败: {e}")
        return {"in_degree": 0, "out_degree": 0, "neo4j_available": True}

    def top_connected_nodes(self, ontology_id: str, limit: int = 10) -> list[dict]:
        """返回连接数最多的 Top-N 节点（度中心性简化版）"""
        if not self._neo4j.available:
            return []
        query = """
        MATCH (n)-[r]-(neighbor)
        WHERE n.ontology_id = $ontology_id AND neighbor.ontology_id = $ontology_id
        RETURN elementId(n) AS node_id,
               labels(n) AS labels,
               n.name_cn AS name,
               count(r) AS degree
        ORDER BY degree DESC
        LIMIT $limit
        """
        try:
            return self._neo4j.run_cypher(query, {"ontology_id": ontology_id, "limit": limit})
        except Exception as e:
            logger.warning(f"top_connected_nodes 失败: {e}")
            return []
