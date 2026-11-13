from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user, require_editor
from app.models.user import User
from app.services.ontology_access import GrantDenied, require_project_grant
from app.services.publication.working_copy import OntologyWorkingCopyService
from app.services.publication.legacy_sync import delete_action_mirror, sync_action_to_v2
from app.models.action import Action
from app.schemas.action import ActionCreate, ActionUpdate, ActionOut
import uuid

router = APIRouter()


def _grant(db: Session, actor: User, ontology_id: str, capability: str) -> dict:
    try:
        return require_project_grant(db, actor, ontology_id, capability)
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc

@router.get("")
def list_actions(ontology_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _grant(db, current_user, ontology_id, "read")
    items = db.query(Action).filter(Action.ontology_id == ontology_id).all()
    return {"data": [ActionOut.model_validate(a).model_dump() for a in items]}

@router.post("", status_code=201)
def create_action(ontology_id: str, body: ActionCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    _grant(db, current_user, ontology_id, "edit")
    def _write():
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        a = Action(id=str(uuid.uuid4()), ontology_id=ontology_id, **data)
        db.add(a); db.flush()
        # mirror into v2_ontology_action_types: publication and the Agent tool
        # catalog read only the v2 table, never this one (see legacy_sync.py)
        sync_action_to_v2(db, ontology_id, a)
        db.flush()
        return {"data": ActionOut.model_validate(a).model_dump()}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="action.create", callback=_write)

@router.get("/{action_id}")
def get_action(ontology_id: str, action_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _grant(db, current_user, ontology_id, "read")
    a = db.query(Action).filter(Action.id == action_id, Action.ontology_id == ontology_id).first()
    if not a:
        raise HTTPException(404, "Not found")
    return {"data": ActionOut.model_validate(a).model_dump()}

@router.put("/{action_id}")
def update_action(ontology_id: str, action_id: str, body: ActionUpdate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    _grant(db, current_user, ontology_id, "edit")
    a = db.query(Action).filter(Action.id == action_id, Action.ontology_id == ontology_id).first()
    if not a:
        raise HTTPException(404, "Not found")
    def _write():
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(a, k, v)
        db.flush()
        sync_action_to_v2(db, ontology_id, a)
        db.flush()
        return {"data": ActionOut.model_validate(a).model_dump()}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="action.update", callback=_write)

@router.delete("/{action_id}", status_code=204)
def delete_action(ontology_id: str, action_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    _grant(db, current_user, ontology_id, "edit")
    a = db.query(Action).filter(Action.id == action_id, Action.ontology_id == ontology_id).first()
    if not a:
        raise HTTPException(404, "Not found")
    def _write():
        delete_action_mirror(db, ontology_id, a)
        db.delete(a); db.flush()
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="action.delete", callback=_write)


@router.post("/{action_id}/toggle")
def toggle_action(ontology_id: str, action_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    _grant(db, current_user, ontology_id, "edit")
    a = db.query(Action).filter(Action.id == action_id, Action.ontology_id == ontology_id).first()
    if not a:
        raise HTTPException(404, "Not found")
    def _write():
        a.enabled = not getattr(a, 'enabled', True)
        sync_action_to_v2(db, ontology_id, a)
        db.flush()
        return {"enabled": a.enabled}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="action.toggle", callback=_write)


@router.post("/publish")
def publish_actions(ontology_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """Direct tool publication is closed; use the Ontology compiler (P1C)."""
    _grant(db, current_user, ontology_id, "edit")
    raise HTTPException(status_code=403, detail="PUBLICATION_NOT_ENABLED")
