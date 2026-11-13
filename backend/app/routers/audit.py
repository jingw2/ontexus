from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user, require_editor
from app.models.audit_task import AuditTask
from app.models.ontology import OntologyProject
from app.schemas.audit import AuditRequest, AuditTaskOut
from app.services.publication.extraction_model_port import default_extraction_model_port
from app.models.user import User
from app.services.ontology_access import GrantDenied, require_project_grant
import uuid

router = APIRouter()


@router.post("")
def start_audit(ontology_id: str, body: AuditRequest, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    try:
        require_project_grant(db, current_user, ontology_id, "edit")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "Ontology not found")
    # R-1: reject an unknown model id with a stable error instead of a 500 FK violation
    if default_extraction_model_port.resolve_model(db, body.model_id) is None:
        raise HTTPException(status_code=422, detail="MODEL_CONFIG_NOT_FOUND")

    task = AuditTask(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        model_id=body.model_id,
        model_name=body.model_name,
        status="queued",
        progress={"stage": "queued", "pct": 0},
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        from app.tasks.audit import run_audit
        run_audit.delay(task.id)
    except Exception:
        import threading

        def run_sync():
            from app.tasks.audit import run_audit
            try:
                run_audit(task.id)
            except Exception:
                pass

        threading.Thread(target=run_sync, daemon=True).start()

    return {"data": {"task_id": task.id}, "message": "Audit queued"}


@router.get("/status")
def get_audit_status(ontology_id: str, task_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        require_project_grant(db, current_user, ontology_id, "read")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc
    task = db.query(AuditTask).filter(AuditTask.id == task_id, AuditTask.ontology_id == ontology_id).first()
    if not task:
        raise HTTPException(404, "Audit task not found")
    return {"data": AuditTaskOut.model_validate(task).model_dump()}
