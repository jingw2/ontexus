"""v2 Search API — 关键词/语义统一搜索"""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import cast, or_, String as SAString
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.deps import get_current_user
from app.models.entity import Entity
from app.models.user import User
from app.services.ontology_access import GrantDenied, require_project_grant

router = APIRouter(dependencies=[Depends(get_current_user)])


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


class SearchRequest(BaseModel):
    query: str
    mode: str = "keyword"  # keyword | semantic
    entity_type: str | None = None
    n_results: int = 10


def _sql_keyword_search(db: Session, ontology_id: str, q: str, n: int) -> list[dict]:
    """SQL 关键词回退搜索 — ChromaDB 不可用时按名称/描述/属性模糊匹配"""
    pattern = f"%{q}%"
    rows = db.query(Entity).filter(
        Entity.ontology_id == ontology_id,
        or_(
            Entity.name_cn.ilike(pattern),
            Entity.name_en.ilike(pattern),
            Entity.description.ilike(pattern),
            cast(Entity.properties, SAString).ilike(pattern),
        ),
    ).limit(n).all()
    return [
        {
            "id": e.id,
            "document": e.description or e.name_cn,
            "metadata": {
                "name_cn": e.name_cn,
                "name_en": e.name_en,
                "entity_type": e.type,
                "properties": e.properties or {},
            },
        }
        for e in rows
    ]


@router.get("/{ontology_id}/search/keyword", dependencies=[Depends(_require_read)])
def keyword_search(
    ontology_id: str,
    q: str = Query(..., description="搜索词"),
    n: int = Query(20, description="结果数"),
    db: Session = Depends(get_db),
):
    """关键词搜索 — 优先 ChromaDB，不可用时回退 SQL"""
    from app.services.v2.vector.chroma_service import ChromaService
    svc = ChromaService()
    if not svc.available:
        results = _sql_keyword_search(db, ontology_id, q, n)
        return {"results": results, "chroma_available": False, "query": q}
    results = svc.keyword_search(ontology_id, q, n_results=n)
    return {"results": results, "chroma_available": True, "query": q}


@router.get("/{ontology_id}/search/semantic", dependencies=[Depends(_require_read)])
def semantic_search(
    ontology_id: str,
    q: str = Query(..., description="搜索词"),
    n: int = Query(10, description="结果数"),
    entity_type: str | None = Query(None, description="实体类型过滤"),
):
    """语义搜索（向量相似度）"""
    from app.services.v2.vector.chroma_service import ChromaService
    svc = ChromaService()
    if not svc.available:
        return {"results": [], "chroma_available": False}
    results = svc.semantic_search(ontology_id, q, n_results=n, entity_type=entity_type)
    return {"results": results, "chroma_available": True, "query": q}


@router.post("/{ontology_id}/search", dependencies=[Depends(_require_read)])
def unified_search(ontology_id: str, body: SearchRequest, db: Session = Depends(get_db)):
    """统一搜索端点"""
    from app.services.v2.vector.chroma_service import ChromaService
    svc = ChromaService()
    if not svc.available:
        if body.mode == "keyword":
            results = _sql_keyword_search(db, ontology_id, body.query, body.n_results)
            return {"results": results, "chroma_available": False, "mode": body.mode}
        return {"results": [], "chroma_available": False, "mode": body.mode}

    if body.mode == "semantic":
        results = svc.semantic_search(
            ontology_id, body.query,
            n_results=body.n_results,
            entity_type=body.entity_type,
        )
    else:
        results = svc.keyword_search(ontology_id, body.query, n_results=body.n_results)

    return {"results": results, "chroma_available": True, "mode": body.mode}
