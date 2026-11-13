import os
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user, require_admin, require_editor
from app.models.file import UploadedFile
from app.models.ontology import OntologyProject
from app.schemas.file import FileOut
from app.services.document_service import convert_document, is_usable_converted_text
from app.services.ontology_access import GrantDenied, require_project_grant
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/png", "image/jpeg", "text/markdown", "text/plain",
    "application/msword", "application/vnd.ms-excel",
}

def _conversion_error_message(converted_md: str | None) -> str | None:
    if is_usable_converted_text(converted_md):
        return None
    if not converted_md:
        return "文件转换失败或无文本内容"
    text = converted_md.strip()
    if text.startswith("[File conversion failed:"):
        return "DOCX/PDF 转换失败，请重新上传或改用 .md / .txt"
    if text.startswith("["):
        return text.split("\n", 1)[0].strip("[]")[:200]
    return text[:200]


def _file_out(f: UploadedFile) -> dict:
    ok = is_usable_converted_text(f.converted_md)
    return FileOut.model_validate(f).model_copy(
        update={"conversion_ok": ok, "conversion_error": _conversion_error_message(f.converted_md)}
    ).model_dump()


def _grant(db: Session, actor, ontology_id: str, capability: str) -> dict:
    try:
        return require_project_grant(db, actor, ontology_id, capability)
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc


@router.get("")
def list_files(ontology_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _grant(db, current_user, ontology_id, "read")
    files = db.query(UploadedFile).filter(UploadedFile.ontology_id == ontology_id).all()
    return {"data": [_file_out(f) for f in files]}

@router.post("", status_code=201)
async def upload_file(
    ontology_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_editor),
):
    _grant(db, current_user, ontology_id, "edit")
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, detail="ONTOLOGY_NOT_FOUND")

    ext_name = (file.filename or "").rsplit(".", 1)[-1].lower()
    allowed = {e.strip() for e in settings.allowed_upload_extensions.split(",") if e.strip()}
    if ext_name not in allowed:
        raise HTTPException(400, f"不支持的文件类型: .{ext_name} (允许: {settings.allowed_upload_extensions})")

    upload_dir = os.path.join(settings.uploads_dir, ontology_id)
    os.makedirs(upload_dir, exist_ok=True)

    file_id = str(uuid.uuid4())
    ext = os.path.splitext(file.filename)[1]
    save_path = os.path.join(upload_dir, f"{file_id}{ext}")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    declared_size = getattr(file, "size", None)
    if declared_size is not None and declared_size > max_bytes:
        raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")

    committed = False
    try:
        total_bytes = 0
        with open(save_path, "wb") as saved:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")
                saved.write(chunk)

        mime = file.content_type or "application/octet-stream"
        conversion = convert_document(save_path, mime)
        if not conversion.ok:
            raise HTTPException(422, conversion.error or "文件转换失败")

        db_file = UploadedFile(
            id=file_id,
            ontology_id=ontology_id,
            filename=file.filename,
            file_path=save_path,
            file_size=total_bytes,
            mime_type=mime,
            converted_md=conversion.content,
        )
        db.add(db_file)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        committed = True
        db.refresh(db_file)
        return {"data": _file_out(db_file)}
    except Exception:
        if not committed and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except OSError:
                logger.warning("failed to clean up rejected upload %s", save_path, exc_info=True)
        raise

@router.delete("/{file_id}", status_code=204)
def delete_file(ontology_id: str, file_id: str, db: Session = Depends(get_db), current_user=Depends(require_admin)):
    _grant(db, current_user, ontology_id, "edit")
    f = db.query(UploadedFile).filter(UploadedFile.id == file_id, UploadedFile.ontology_id == ontology_id).first()
    if not f:
        raise HTTPException(404, "File not found")
    file_path = f.file_path
    db.delete(f)
    db.commit()
    if os.path.exists(file_path):
        os.remove(file_path)
