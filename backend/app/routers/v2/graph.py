"""v2 Graph API — 基于 Neo4j"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.services.publication.working_copy import OntologyWorkingCopyService
from app.services.relation_dedup import dedupe_relation_edges as _dedupe_relation_edges
from app.database import SessionLocal
from sqlalchemy.orm import Session
from app.services.ontology_access import GrantDenied, require_project_grant

router = APIRouter(dependencies=[Depends(get_current_user)])


def get_neo4j():
    from app.services.v2.graph.neo4j_service import Neo4jService
    return Neo4jService()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_read(ontology_id: str, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    try:
        require_project_grant(db, actor, ontology_id, "read")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc


def _require_edit(ontology_id: str, db: Session = Depends(get_db), actor: User = Depends(require_editor)):
    try:
        require_project_grant(db, actor, ontology_id, "edit")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc


class CypherRequest(BaseModel):
    query: str
    params: dict = {}


@router.get("/{ontology_id}/graph", dependencies=[Depends(_require_read)])
def get_graph(ontology_id: str, limit: int = 200, label_filter: str | None = None):
    """返回本体图谱数据 (Neovis.js 兼容格式)"""
    svc = get_neo4j()
    if not svc.available:
        return _sqlite_graph_data(ontology_id, limit=limit, label_filter=label_filter)
    try:
        data = svc.get_graph_data(ontology_id, limit=limit, label_filter=label_filter)
    except Exception:
        # 共享 driver 缓存期间 Neo4j 宕机 → 回退 SQLite 而非 500
        svc.close()
        return _sqlite_graph_data(ontology_id, limit=limit, label_filter=label_filter)
    svc.close()
    # Neo4j 可用但该 ontology 无数据（如简易 LLM 路线未同步写入）→ 回退 SQLite
    if not data.get("nodes"):
        return _sqlite_graph_data(ontology_id, limit=limit, label_filter=label_filter)
    data["neo4j_available"] = True
    return data


def _sqlite_graph_data(ontology_id: str, limit: int = 200, label_filter: str | None = None) -> dict:
    from app.models.entity import Entity
    from app.models.relation import Relation

    db = SessionLocal()
    try:
        query = db.query(Entity).filter(Entity.ontology_id == ontology_id)
        if label_filter:
            query = query.filter(Entity.type == label_filter)
        entities = query.limit(limit).all()
        entity_ids = {e.id for e in entities}
        relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
        edges = _dedupe_relation_edges(
            r for r in relations
            if r.source_entity in entity_ids and r.target_entity in entity_ids
        )
        nodes = [
            {
                "id": e.id,
                "labels": [e.type or "OntologyEntity"],
                "properties": {
                    **(e.properties or {}),
                    "id": e.id,
                    "source_id": e.id,
                    "ontology_id": ontology_id,
                    "name_cn": e.name_cn or "",
                    "name_en": e.name_en or "",
                    "name": e.name_cn or e.name_en or e.id,
                    "type": e.type or "",
                    "description": e.description or "",
                    "confidence": e.confidence or 1.0,
                    "version": e.version or "v0.1",
                },
            }
            for e in entities
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "neo4j_available": False,
            "fallback": "sqlite",
        }
    finally:
        db.close()


@router.get("/{ontology_id}/graph/quality", dependencies=[Depends(_require_read)])
def graph_quality(ontology_id: str):
    from app.models.entity import Entity
    from app.models.relation import Relation
    from collections import Counter

    db = SessionLocal()
    try:
        entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
        relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
        entity_ids = {e.id for e in entities}
        connected_ids = {r.source_entity for r in relations} | {r.target_entity for r in relations}
        orphan_relations = [
            r.id for r in relations
            if r.source_entity not in entity_ids or r.target_entity not in entity_ids
        ]
        isolated = [e.id for e in entities if e.id not in connected_ids]
        names = [e.name_cn for e in entities if e.name_cn]
        duplicate_names = {name: count for name, count in Counter(names).items() if count > 1}
        object_types = Counter(e.type or "Entity" for e in entities)
        relation_types = Counter(r.type or "RELATED" for r in relations)
        node_count = len(entities)
        edge_count = len(relations)
        duplicate_name_instances = sum(duplicate_names.values())
        quality_score = 1.0
        if node_count:
            quality_score -= min(0.4, len(isolated) / node_count * 0.4)
            quality_score -= min(0.25, duplicate_name_instances / node_count * 0.25)
        if edge_count:
            quality_score -= min(0.25, len(orphan_relations) / edge_count * 0.25)
        return {
            "ontology_id": ontology_id,
            "node_count": node_count,
            "edge_count": edge_count,
            "isolated_node_count": len(isolated),
            "orphan_relation_count": len(orphan_relations),
            "duplicate_display_name_count": duplicate_name_instances,
            "object_type_counts": dict(object_types),
            "relation_type_counts": dict(relation_types),
            "relation_density": round(edge_count / node_count, 4) if node_count else 0,
            "quality_score": round(max(0.0, quality_score), 4),
            "samples": {
                "isolated_node_ids": isolated[:10],
                "orphan_relation_ids": orphan_relations[:10],
                "duplicate_display_names": dict(list(duplicate_names.items())[:10]),
            },
        }
    finally:
        db.close()


@router.get("/{ontology_id}/integrations/status", dependencies=[Depends(_require_read)])
def integration_status(ontology_id: str):
    neo = get_neo4j()
    neo_available = neo.available
    if neo_available:
        neo.close()
    from app.services.v2.vector.chroma_service import ChromaService
    chroma = ChromaService()
    return {
        "ontology_id": ontology_id,
        "neo4j": {"available": neo_available},
        "chroma": {"available": chroma.available, "entity_count": chroma.count(ontology_id)},
    }


@router.post("/{ontology_id}/graph/cypher", dependencies=[Depends(_require_read)])
def run_cypher(ontology_id: str, body: CypherRequest):
    """执行 Cypher 查询 (只读校验 + 强制 ontology_id 过滤)"""
    from app.services.v2.graph.cypher_builder import validate_readonly_cypher

    error = validate_readonly_cypher(body.query)
    if error:
        raise HTTPException(400, error)

    svc = get_neo4j()
    if not svc.available:
        return {"results": [], "neo4j_available": False}
    params = dict(body.params or {})
    params["ontology_id"] = ontology_id  # 供查询中的 $ontology_id 使用, 防跨本体读取
    results = svc.run_cypher(body.query, params)
    svc.close()
    return {"results": results, "neo4j_available": True}


@router.get("/{ontology_id}/graph/neighbors/{node_id}", dependencies=[Depends(_require_read)])
def get_neighbors(ontology_id: str, node_id: str, depth: int = 1):
    """查询节点邻居"""
    svc = get_neo4j()
    if not svc.available:
        return {"nodes": [], "edges": [], "neo4j_available": False}
    query = f"""
    MATCH path = (n)-[r*1..{min(depth, 5)}]-(m)
    WHERE elementId(n) = $node_id AND n.ontology_id = $ontology_id
      AND all(node IN nodes(path) WHERE node.ontology_id = $ontology_id)
    RETURN n, r, m LIMIT 100
    """
    results = svc.run_cypher(query, {"node_id": node_id, "ontology_id": ontology_id})
    svc.close()
    return {"results": results, "neo4j_available": True}


# ── 自然语言查询 ──────────────────────────────────────────────────────

class NLQueryRequest(BaseModel):
    question: str
    schema: dict = {}


@router.post("/{ontology_id}/graph/ask", dependencies=[Depends(_require_read)])
def nl_query(ontology_id: str, body: NLQueryRequest):
    """自然语言 → Cypher → 图数据"""
    from app.services.v2.graph.nl2cypher import NL2CypherService
    nl_svc = NL2CypherService()
    plan = nl_svc.translate(body.question, body.schema)

    svc = get_neo4j()
    if not svc.available:
        return {"results": [], "cypher": plan.cypher, "explanation": plan.explanation, "neo4j_available": False}

    try:
        results = svc.run_cypher(plan.cypher, {"ontology_id": ontology_id})
        svc.close()
        return {
            "results": results,
            "cypher": plan.cypher,
            "explanation": plan.explanation,
            "confidence": plan.confidence,
            "neo4j_available": True,
        }
    except Exception as e:
        svc.close()
        return {"results": [], "cypher": plan.cypher, "error": str(e), "neo4j_available": True}


# ── 高级图分析 ─────────────────────────────────────────────────────────

@router.get("/{ontology_id}/graph/path", dependencies=[Depends(_require_read)])
def graph_path(ontology_id: str, src: str, tgt: str):
    """两节点间最短路径"""
    from app.services.v2.graph.graph_analytics import GraphAnalyticsService
    svc = GraphAnalyticsService()
    return svc.shortest_path(ontology_id, src, tgt)


@router.get("/{ontology_id}/graph/degree/{node_id}", dependencies=[Depends(_require_read)])
def node_degree(ontology_id: str, node_id: str):
    """查询节点度数（入度 + 出度）"""
    from app.services.v2.graph.graph_analytics import GraphAnalyticsService
    svc = GraphAnalyticsService()
    return svc.node_degree(ontology_id, node_id)


@router.get("/{ontology_id}/graph/top-nodes", dependencies=[Depends(_require_read)])
def top_nodes(ontology_id: str, limit: int = 10):
    """返回连接数最多的 Top-N 节点"""
    from app.services.v2.graph.graph_analytics import GraphAnalyticsService
    svc = GraphAnalyticsService()
    return {"nodes": svc.top_connected_nodes(ontology_id, limit)}


@router.post("/{ontology_id}/graph/sync", dependencies=[Depends(_require_edit)])
def sync_graph(ontology_id: str, current_user: User = Depends(require_editor)):
    """将 SQLite 实体/关系全量同步到 Neo4j"""
    from app.database import SessionLocal
    from app.models.entity import Entity
    from app.models.relation import Relation

    neo = get_neo4j()
    if not neo.available:
        return {"synced": False, "reason": "Neo4j unavailable"}

    db = SessionLocal()
    try:
        entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
        relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()

        # Build entity id -> neo4j label map (use type as label, fallback Entity)
        entity_label_map: dict[str, str] = {}

        # Batch upsert entities
        batch = []
        for e in entities:
            label = (e.type or "Entity").replace(" ", "_")
            entity_label_map[e.id] = label
            props = {
                **(e.properties or {}),
                "id": e.id,           # SQLite UUID 优先，覆盖 properties 里的 id
                "source_id": e.id,
                "ontology_id": ontology_id,
                "name_cn": e.name_cn or "",
                "name": e.name_cn or "",
                "name_en": e.name_en or "",
                "type": e.type or "",
                "description": e.description or "",
                "confidence": e.confidence or 1.0,
                "version": e.version or "v0.1",
            }
            # Use generic label for batch
            batch.append(props)

        # Upsert all as generic "OntologyEntity" first (fast batch)
        synced_entities = neo.batch_upsert_entities("OntologyEntity", batch, key_field="id")

        # Upsert relations
        synced_relations = 0
        for r in relations:
            src_label = entity_label_map.get(r.source_entity, "OntologyEntity")
            tgt_label = entity_label_map.get(r.target_entity, "OntologyEntity")
            rel_type = (r.type or "RELATED").upper().replace(" ", "_").replace("-", "_")
            ok = neo.upsert_relation(
                "OntologyEntity", r.source_entity,
                "OntologyEntity", r.target_entity,
                rel_type,
                props={"ontology_id": ontology_id, "confidence": r.confidence or 1.0},
            )
            if ok:
                synced_relations += 1

        neo.close()
        return {
            "synced": True,
            "entities": synced_entities,
            "relations": synced_relations,
            "ontology_id": ontology_id,
        }
    finally:
        db.close()
