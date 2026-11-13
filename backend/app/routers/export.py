from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user
from app.services import export_service
from app.services.ontology_access import GrantDenied, require_project_grant

router = APIRouter()

FORMAT_MAP = {
    "json": ("application/json", "json", export_service.export_json),
    "yaml": ("application/x-yaml", "yaml", export_service.export_yaml),
    "csv": ("text/csv", "csv", export_service.export_csv),
    "ttl": ("text/turtle", "ttl", export_service.export_ttl),
    "html": ("text/html", "html", export_service.export_html),
    "cypher": ("text/plain", "cypher", export_service.export_neo4j_cypher),
    "tugraph": ("application/zip", "zip", export_service.export_tugraph_zip),
}

@router.get("")
def export_ontology(
    ontology_id: str,
    format: str = Query("json", pattern="^(json|yaml|csv|ttl|html|cypher|tugraph)$"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    try:
        require_project_grant(db, current_user, ontology_id, "read")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc
    if format not in FORMAT_MAP:
        raise HTTPException(400, f"Unsupported format: {format}")
    content_type, ext, fn = FORMAT_MAP[format]
    try:
        content = fn(db, ontology_id)
    except Exception as e:
        raise HTTPException(500, f"Export failed: {e}")
    return Response(
        content=content.encode("utf-8") if isinstance(content, str) else content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="ontology_{ontology_id}.{ext}"'}
    )
