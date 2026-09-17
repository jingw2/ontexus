"""v2 Curated Dataset API — reads from v2_datasets kind=curated"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from app.database import SessionLocal
from app.deps import get_current_user, require_admin, require_editor
from app.models.v2.curated import CuratedDataset, CuratedReview

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CuratedDatasetResponse(BaseModel):
    id: str
    name: str
    status: str
    row_count: Optional[int] = None
    quality_score: Optional[float] = None

    class Config:
        from_attributes = True


@router.get("", response_model=list[CuratedDatasetResponse])
def list_curated(db: Session = Depends(get_db)):
    """列出所有 Curated Dataset（从 v2_datasets 读 kind=curated）"""
    from app.models.v2.dataset import Dataset, DatasetVersion
    rows = db.query(Dataset).filter(Dataset.kind == "curated").order_by(Dataset.created_at.desc()).all()
    # Batch fetch all reviews for displayed datasets to avoid N+1
    dataset_ids = [r.id for r in rows]
    all_reviews = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id.in_(dataset_ids)
    ).order_by(CuratedReview.created_at.desc()).all()

    # Build dict: dataset_id -> latest review
    review_by_dataset: dict = {}
    for rev in all_reviews:
        if rev.curated_dataset_id not in review_by_dataset:
            review_by_dataset[rev.curated_dataset_id] = rev

    result = []
    for r in rows:
        ver = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == r.id
        ).order_by(DatasetVersion.version_no.desc()).first()
        # 从 schema_json 读质量分
        quality = None
        if r.schema_json and isinstance(r.schema_json, dict):
            quality = r.schema_json.get("quality_score")
        review = review_by_dataset.get(r.id)
        real_status = review.status if review else "pending_review"
        result.append(CuratedDatasetResponse(
            id=r.id, name=r.name,
            status=real_status,
            row_count=ver.rowcount if ver else None,
            quality_score=quality,
        ))
    return result


@router.delete("/{dataset_id}", status_code=204)
def delete_curated(dataset_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """删除 Curated Dataset 及其版本数据（仅管理员）"""
    from app.models.v2.dataset import Dataset, DatasetVersion, MediaItem
    ds = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
    if not ds:
        raise HTTPException(404, "Curated dataset not found")
    # 清理关联版本和媒体项
    ver_ids = [v.id for v in db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).all()]
    media_uris: list[str] = []
    if ver_ids:
        media_items = db.query(MediaItem).filter(MediaItem.dataset_version_id.in_(ver_ids)).all()
        for m in media_items:
            media_uris.append(m.storage_uri)
            if m.ocr_result_uri:
                media_uris.append(m.ocr_result_uri)
        db.query(MediaItem).filter(MediaItem.dataset_version_id.in_(ver_ids)).delete(synchronize_session=False)
    db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).delete(synchronize_session=False)
    # 清理审核记录
    db.query(CuratedReview).filter(CuratedReview.curated_dataset_id == dataset_id).delete(synchronize_session=False)
    db.delete(ds)
    db.commit()

    # Best-effort MinIO cleanup — never fails an already-committed delete
    if media_uris:
        from app.services.storage_service import get_storage_service
        storage = get_storage_service()
        for uri in media_uris:
            try:
                storage.delete_object(uri)
            except Exception:
                logger.warning("MinIO cleanup failed for object %s (dataset %s)", uri, dataset_id, exc_info=True)


@router.get("/{dataset_id}", response_model=CuratedDatasetResponse)
def get_curated(dataset_id: str, db: Session = Depends(get_db)):
    # 先查旧 curated 表，再查 v2_datasets
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if not ds:
        from app.models.v2.dataset import Dataset
        d2 = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if d2:
            return CuratedDatasetResponse(id=d2.id, name=d2.name, status="pending_review")
    if not ds:
        raise HTTPException(404, "Curated dataset not found")
    return ds


@router.get("/{dataset_id}/preview")
def preview_curated(dataset_id: str, limit: int = 100, db: Session = Depends(get_db)):
    """数据预览 — 从 v2_datasets 存储读取实际数据行"""
    from app.services.v2.dataset_service import DatasetService
    from app.models.v2.dataset import Dataset as Ds2

    # 尝试旧 curated 表
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if ds:
        name = ds.name
    else:
        d2 = db.query(Ds2).filter(Ds2.id == dataset_id, Ds2.kind == "curated").first()
        if not d2:
            raise HTTPException(404, "Curated dataset not found")
        name = d2.name

    # 读最新版本数据
    try:
        svc = DatasetService(db)
        rows = svc.preview(dataset_id, 1, limit=limit)
        return {"dataset_id": dataset_id, "name": name, "rows": rows, "count": len(rows)}
    except Exception as e:
        return {"dataset_id": dataset_id, "name": name, "rows": [], "count": 0, "error": str(e)}


@router.get("/{dataset_id}/quality")
def get_quality_report(dataset_id: str, db: Session = Depends(get_db)):
    """获取质量报告（支持旧 curated 表和新 v2_datasets curated）"""
    from app.services.v2.curated.quality_service import QualityService
    from app.services.v2.dataset_service import DatasetService

    # 查旧 curated 表
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    sample_data = []
    if ds:
        if ds.schema_json and isinstance(ds.schema_json, dict):
            sample_data = ds.schema_json.get("sample_rows", [])
    else:
        # 尝试从 v2_datasets 读取样本数据
        from app.models.v2.dataset import Dataset as Ds2
        d2 = db.query(Ds2).filter(Ds2.id == dataset_id, Ds2.kind == "curated").first()
        if not d2:
            raise HTTPException(404, "Curated dataset not found")
        # 读最新版本数据作为样本
        try:
            svc2 = DatasetService(db)
            sample_data = svc2.preview(dataset_id, 1, limit=200)
        except Exception:
            sample_data = []

    svc = QualityService(db)
    report = svc.compute_report(dataset_id, sample_data)
    return report.to_dict()


@router.post("/{dataset_id}/review")
def submit_review(
    dataset_id: str,
    action: str,  # "approve" | "reject"
    notes: str = "",
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),  # PRD Security Logic: only admin can approve curated rows
):
    """提交审核结果（approve/reject）"""
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if not ds:
        from app.models.v2.dataset import Dataset
        ds_v2 = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if not ds_v2:
            raise HTTPException(404, "Curated dataset not found")
        # Pipeline creates curated data in v2_datasets, but CuratedReview FK points to
        # v2_curated_datasets. If the CuratedDataset row doesn't exist yet, sync it now
        # so the FK constraint doesn't fail on PostgreSQL (SQLite silently ignores FK).
        ds = CuratedDataset(
            id=ds_v2.id,
            name=ds_v2.name,
            pipeline_id=getattr(ds_v2, "pipeline_id", None),
            schema_json=ds_v2.schema_json,
            status="draft",
        )
        db.add(ds)
        db.flush()

    from datetime import datetime, timezone
    review = CuratedReview(
        curated_dataset_id=dataset_id,
        status="approved" if action == "approve" else "rejected",
        notes=notes,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(review)
    if ds:
        ds.status = "approved" if action == "approve" else "rejected"
    db.commit()
    db.refresh(review)

    if action == "approve":
        try:
            from app.services.v2.incremental.orchestrator import IncrementalOrchestrator
            orch = IncrementalOrchestrator(db)
            orch.on_review_approved(review.id)
        except Exception as e:
            logger.warning(f"Mapping trigger failed after review approve {review.id}: {e}")

    return {"review_id": review.id, "status": review.status}


# ── 审核工作流端点 ─────────────────────────────────────────────────────

class BatchEditRequest(BaseModel):
    edits: list[dict]  # [{row_pk, field_name, old_value, new_value}]


class PipelineRunBinding(BaseModel):
    # Optional: the human curated-review UI (`frontend/src/api/v2/curated.ts`)
    # sends no body at all -- it has no concept of "which pipeline run", and
    # predates this binding. Only the business-journey eval harness
    # (`evals.business_journeys.api_client.JourneyApiClient`) supplies this,
    # to prove its own review/approval is bound to the run it produced.
    pipeline_run_id: str | None = None


def _require_run_curated_dataset(db: Session, pipeline_run_id: str, dataset_id: str) -> None:
    from app.models.v2.pipeline import PipelineRun

    run = db.get(PipelineRun, pipeline_run_id)
    stats = run.stats or {} if run else {}
    outputs = {str(stats.get("curated_dataset_id") or "")}
    listed_outputs = stats.get("curated_dataset_ids") or ()
    if isinstance(listed_outputs, (list, tuple)):
        outputs.update(str(output) for output in listed_outputs if output)
    if run is None or not run.is_governed or dataset_id not in outputs:
        raise HTTPException(status_code=422, detail="CURATED_DATASET_RUN_MISMATCH")


def _ensure_curated_dataset_row(db: Session, dataset_id: str) -> None:
    """`ReviewService` (and therefore this whole review workflow) reads and
    writes `CuratedDataset`/`v2_curated_datasets` — a separate table from
    the `Dataset`/`v2_datasets` (`kind="curated"`) rows the real Pipeline DAG
    engine (`app.tasks.v2.pipeline_run._save_curated_dataset`) actually
    creates. Nothing links the two today, so `POST /{dataset_id}/reviews`
    404s for every dataset `GET /api/v2/curated` (which reads `Dataset`) ever
    lists — confirmed directly by driving a real pipeline run end to end.
    Bridging them by reusing the SAME id (both are UUID-keyed, and nothing
    else currently reads or writes `CuratedDataset.id` as anything other
    than an opaque primary key) is the minimal fix: auto-provision the
    corresponding `CuratedDataset` row, from the real `Dataset`'s own
    name/schema metadata, the first time a review is started for it."""
    from app.models.v2.dataset import Dataset, DatasetVersion

    existing = db.get(CuratedDataset, dataset_id)
    if existing is not None:
        return
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.kind != "curated":
        raise HTTPException(404, f"Curated dataset {dataset_id} not found")
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id
    ).order_by(DatasetVersion.version_no.desc()).first()
    quality_score = None
    if dataset.schema_json and isinstance(dataset.schema_json, dict):
        quality_score = dataset.schema_json.get("quality_score")
    db.add(CuratedDataset(
        id=dataset.id, name=dataset.name, schema_json=dataset.schema_json,
        latest_version_id=version.id if version else None, quality_score=quality_score,
        status="pending_review",
    ))
    db.commit()


@router.post("/{dataset_id}/reviews")
def start_review(
    dataset_id: str, body: PipelineRunBinding | None = None,
    db: Session = Depends(get_db), _=Depends(require_editor),
):
    """为数据集启动审核流程"""
    from app.services.v2.curated.review_service import ReviewService
    pipeline_run_id = body.pipeline_run_id if body else None
    if pipeline_run_id:
        _require_run_curated_dataset(db, pipeline_run_id, dataset_id)
    _ensure_curated_dataset_row(db, dataset_id)
    svc = ReviewService(db)
    review = svc.start_review(dataset_id)
    review.pipeline_run_id = pipeline_run_id
    db.commit()
    return {"review_id": review.id, "status": review.status}


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db)):
    """获取审核记录详情"""
    from app.models.v2.curated import CuratedReview
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
    if not review:
        raise HTTPException(404, "Review not found")
    return {
        "id": review.id,
        "curated_dataset_id": review.curated_dataset_id,
        "pipeline_run_id": review.pipeline_run_id,
        "status": review.status,
        "notes": review.notes,
        "decided_at": review.decided_at,
    }


@router.post("/reviews/{review_id}/edits")
def add_edit(review_id: str, body: BatchEditRequest, db: Session = Depends(get_db), _=Depends(require_editor)):
    """批量提交行编辑"""
    from app.services.v2.curated.review_service import ReviewService
    svc = ReviewService(db)
    edits = svc.batch_edit_rows(review_id, body.edits)
    return {"saved": len(edits)}


@router.post("/reviews/{review_id}/approve")
def approve_review(
    review_id: str, body: PipelineRunBinding | None = None, notes: str = "",
    db: Session = Depends(get_db), _admin=Depends(require_admin),
):
    """审核通过"""
    from app.services.v2.curated.review_service import ReviewService
    review_row = db.get(CuratedReview, review_id)
    if review_row is None:
        raise HTTPException(status_code=404, detail="REVIEW_NOT_FOUND")
    pipeline_run_id = body.pipeline_run_id if body else None
    if pipeline_run_id:
        _require_run_curated_dataset(db, pipeline_run_id, review_row.curated_dataset_id)
        if review_row.pipeline_run_id != pipeline_run_id:
            raise HTTPException(status_code=422, detail="CURATED_REVIEW_RUN_MISMATCH")
    svc = ReviewService(db)
    review = svc.approve(review_id, notes)
    return {"review_id": review.id, "status": review.status}


@router.post("/reviews/{review_id}/reject")
def reject_review(review_id: str, notes: str = "", db: Session = Depends(get_db),
                  _admin=Depends(require_admin)):
    """审核拒绝"""
    from app.services.v2.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.reject(review_id, notes)
    return {"review_id": review.id, "status": review.status}
