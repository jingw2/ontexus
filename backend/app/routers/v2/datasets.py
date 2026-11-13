"""v2 Dataset API"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.services.v2.dataset_service import DatasetService

router = APIRouter(dependencies=[Depends(get_current_user)])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class DatasetResponse(BaseModel):
    id: str
    name: str
    kind: str
    class Config:
        from_attributes = True


_UPLOAD_READ_CHUNK_SIZE = 1024 * 1024


async def _read_upload_bounded(
    file: UploadFile,
    max_bytes: int,
    *,
    chunk_size: int = _UPLOAD_READ_CHUNK_SIZE,
    error_detail: str | None = None,
) -> bytes:
    """Read at most one byte beyond the configured upload limit."""
    content = bytearray()
    while len(content) <= max_bytes:
        read_size = min(chunk_size, max_bytes - len(content) + 1)
        chunk = await file.read(read_size)
        if not chunk:
            break
        if len(content) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail=error_detail or "文件超过大小限制")
        content.extend(chunk)
    return bytes(content)


@router.post("/upload", status_code=201)
async def upload_dataset(file: UploadFile = File(...), db: Session = Depends(get_db), _=Depends(require_editor)):
    """上传 CSV/Excel 文件，自动创建 raw Dataset + DatasetVersion"""
    import os
    from app.config import settings

    name = os.path.splitext(file.filename or "upload")[0]
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    allowed = {e.strip() for e in settings.allowed_upload_extensions.split(",") if e.strip()}
    if ext not in allowed:
        raise HTTPException(400, f"不支持的文件类型: .{ext} (允许: {settings.allowed_upload_extensions})")

    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    content = await _read_upload_bounded(
        file,
        max_upload_bytes,
        error_detail=f"文件超过大小限制 {settings.max_upload_mb}MB",
    )
    # 推断 kind
    if ext in ("csv", "xlsx", "xls"):
        kind = "structured"
    elif ext in ("json", "xml"):
        kind = "semi"
    else:
        kind = "unstructured"

    svc = DatasetService(db)
    ds = svc.create_dataset(name=name, kind=kind)
    # 估算行数
    rowcount = None
    if ext == "csv":
        try:
            rowcount = content.count(b"\n")
        except Exception:
            pass
    svc.create_version(ds.id, content, rowcount=rowcount)
    return {"data": {"id": ds.id, "name": ds.name, "kind": ds.kind, "dataset_type": "raw_dataset", "schema_type": "tabular"}}

@router.get("", response_model=list[DatasetResponse])
def list_datasets(kind: str | None = None, db: Session = Depends(get_db)):
    svc = DatasetService(db)
    return svc.list_datasets(kind=kind)

@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds

@router.get("/{dataset_id}/versions")
def list_versions(dataset_id: str, db: Session = Depends(get_db)):
    svc = DatasetService(db)
    versions = svc.list_versions(dataset_id)
    return [{"id": v.id, "version_no": v.version_no, "rowcount": v.rowcount, "storage_uri": v.storage_uri} for v in versions]

@router.get("/{dataset_id}/versions/{version_no}/preview")
def preview_data(dataset_id: str, version_no: int, limit: int = 100, db: Session = Depends(get_db)):
    svc = DatasetService(db)
    return svc.preview(dataset_id, version_no, limit)


@router.get("/{dataset_id}/schema")
def get_schema(dataset_id: str, db: Session = Depends(get_db)):
    """返回数据集的 schema（列名、类型、样本值）"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")

    # Use latest version for schema inference
    versions = svc.list_versions(dataset_id)
    if not versions:
        return {"dataset_id": dataset_id, "columns": []}

    latest_version_no = versions[-1].version_no
    rows = svc.preview(dataset_id, latest_version_no, limit=10)
    if not rows:
        return {"dataset_id": dataset_id, "columns": []}

    columns = []
    all_keys = list(rows[0].keys()) if rows else []
    for key in all_keys:
        sample_values = [row.get(key) for row in rows if row.get(key) is not None][:5]
        # Infer type from sample values
        col_type = "string"
        for val in sample_values:
            if isinstance(val, bool):
                col_type = "boolean"
                break
            elif isinstance(val, int):
                col_type = "integer"
                break
            elif isinstance(val, float):
                col_type = "float"
                break
            elif isinstance(val, str):
                try:
                    int(val)
                    col_type = "integer"
                except ValueError:
                    try:
                        float(val)
                        col_type = "float"
                    except ValueError:
                        col_type = "string"
                break
        columns.append({"name": key, "type": col_type, "sample_values": sample_values})

    return {"dataset_id": dataset_id, "columns": columns}


@router.get("/{dataset_id}/stats")
def get_stats(dataset_id: str, db: Session = Depends(get_db)):
    """返回数据集统计信息"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")

    versions = svc.list_versions(dataset_id)
    version_count = len(versions)

    # Use latest version for row/column counts and null rates
    row_count = 0
    column_count = 0
    null_rates: dict = {}

    if versions:
        latest = versions[-1]
        row_count = latest.rowcount or 0
        rows = svc.preview(dataset_id, latest.version_no, limit=100)
        if rows:
            column_count = len(rows[0].keys())
            # Compute null rates per column
            for key in rows[0].keys():
                null_count = sum(1 for row in rows if row.get(key) is None or row.get(key) == "")
                null_rates[key] = round(null_count / len(rows), 4)

    return {
        "dataset_id": dataset_id,
        "row_count": row_count,
        "column_count": column_count,
        "null_rates": null_rates,
        "version_count": version_count,
    }
