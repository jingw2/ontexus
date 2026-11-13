from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.deps import get_db, get_current_user
from app.models.ontology import OntologyProject
from app.models.user import User
from app.services.ontology_access import discoverable_ontology_ids

router = APIRouter()

def _safe_count(db, model, ontology_ids):
    try:
        return db.query(model).filter(model.ontology_id.in_(ontology_ids)).count()
    except Exception:
        return 0

@router.get("/stats")
def get_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.models.entity import Entity
    from app.models.logic import LogicRule
    from app.models.action import Action

    discoverable_ids = discoverable_ontology_ids(db, current_user)

    # Recent ontologies
    recent = db.query(OntologyProject).filter(
        OntologyProject.id.in_(discoverable_ids)
    ).order_by(OntologyProject.updated_at.desc()).limit(6).all()
    recent_list = []
    for o in recent:
        entity_count = db.query(Entity).filter(Entity.ontology_id == o.id).count()
        logic_count = db.query(LogicRule).filter(LogicRule.ontology_id == o.id).count()
        action_count = db.query(Action).filter(Action.ontology_id == o.id).count()
        recent_list.append({
            "id": o.id,
            "name": o.name,
            "domain": o.domain,
            "status": o.status,
            "entity_count": entity_count,
            "logic_count": logic_count,
            "action_count": action_count,
            "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        })

    # Domain distribution
    domain_rows = (
        db.query(OntologyProject.domain, func.count(OntologyProject.id))
        .filter(OntologyProject.id.in_(discoverable_ids))
        .group_by(OntologyProject.domain)
        .all()
    )
    domain_counts = {row[0]: row[1] for row in domain_rows if row[0]}

    # Status breakdown
    status_rows = (
        db.query(OntologyProject.status, func.count(OntologyProject.id))
        .filter(OntologyProject.id.in_(discoverable_ids))
        .group_by(OntologyProject.status)
        .all()
    )
    status_counts = {row[0]: row[1] for row in status_rows if row[0]}

    return {
        "data": {
            "ontology_count": db.query(OntologyProject).filter(OntologyProject.id.in_(discoverable_ids)).count(),
            "entity_count": _safe_count(db, Entity, discoverable_ids),
            "logic_count": _safe_count(db, LogicRule, discoverable_ids),
            "action_count": _safe_count(db, Action, discoverable_ids),
            "recent_ontologies": recent_list,
            "domain_counts": domain_counts,
            "status_counts": status_counts,
        },
        "message": "ok"
    }
