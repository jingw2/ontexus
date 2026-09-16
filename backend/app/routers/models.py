"""Models API (P2A-CALLERS): redacted tagged union + immutable version surface.

LLM configs expose immutable behavior-version metadata and a versioned N+1
surface; the legacy `PUT /{id}` remains a 201 compatibility alias for
`POST /{id}/versions` with a deprecation header.  OCR/`other` configs keep
their existing redacted shape on the legacy path.  Admin model-migration
remediation lives at `/api/v1/admin/model-migration-remediations`.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user, require_admin, require_editor
from app.models.extraction_task import ExtractionTask
from app.models.model_config import ModelConfig
from app.models.model_version import ModelConfigVersion
from app.models.user import User
from app.schemas.model_config import (
    ArchiveModelMigrationRequest,
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelCredentialOut,
    ModelVersionCreate,
    ModelVersionOut,
    RemediateModelMigrationRequest,
)
from app.services.encryption_service import encrypt
from app.services.model_version import (
    ModelContractInvalid,
    ModelRevisionConflict,
    ModelVersionUnavailable,
    archive_blocked_identity,
    behavior_hash,
    create_next_version,
    legacy_contract_for,
    remediate_blocked_identity,
    rotate_credential,
    select_active_version,
)

router = APIRouter()


def _serialize_config(config: ModelConfig, db: Session) -> dict:
    from app.services.model_version import versioning_schema_present

    out = ModelConfigOut.model_validate(config).model_dump()
    if not versioning_schema_present(db):
        # pre-0004 schema: legacy redacted shape, no version fields
        out.pop("status", None)
        out.pop("versions_count", None)
        out.pop("active_version", None)
        return out
    state = db.execute(text(
        "SELECT status, active_version_id FROM model_configs WHERE id = :id"
    ), {"id": config.id}).mappings().one_or_none()
    status = state["status"] if state else "active"
    out["status"] = status
    out["versions_count"] = db.execute(text(
        "SELECT count(*) FROM model_config_versions WHERE model_config_id = :id"
    ), {"id": config.id}).scalar_one()
    if (config.config_type or "llm") == "llm" and state and state["active_version_id"]:
        version = db.query(ModelConfigVersion).filter(
            ModelConfigVersion.id == state["active_version_id"]
        ).first()
        if version:
            out["active_version"] = {
                "version_no": version.version_no,
                "behavior_hash": version.behavior_hash,
                "conservative_input_limit": version.conservative_input_limit,
                "created_at": version.created_at,
            }
            # the active version's contract is the actual source of truth
            # for which models this identity currently exposes — `config.models`
            # is a denormalized legacy column that write paths must remember
            # to keep in sync, so prefer the contract whenever it says something
            if version.model_contract:
                out["models"] = [
                    entry.get("provider_model_revision") for entry in version.model_contract
                    if entry.get("provider_model_revision")
                ]
    else:
        # OCR/other responses expose their legacy shape: no version field.
        out.pop("active_version", None)
    return out


@router.get("")
def list_models(db: Session = Depends(get_db), _=Depends(get_current_user)):
    configs = db.query(ModelConfig).order_by(ModelConfig.updated_at.desc()).all()
    return {"data": [_serialize_config(c, db) for c in configs]}


@router.post("", status_code=201)
def create_model(body: ModelConfigCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    config = ModelConfig(
        id=str(uuid.uuid4()),
        name=body.name,
        config_type=body.config_type or "llm",
        provider=body.provider,
        api_base=body.api_base,
        api_key_encrypted=encrypt(body.api_key or ""),
        models=body.models or [],
        options=body.options or {},
        created_by=current_user.id,
    )
    db.add(config)
    db.flush()
    from app.services.model_version import versioning_schema_present
    if (body.config_type or "llm") == "llm" and versioning_schema_present(db):
        # Bootstrap an immediate immutable behavior version (legacy-style
        # contract: verified fields unverified, never guessed) + credential.
        contract = legacy_contract_for(body.models or [])
        digest = behavior_hash(body.provider, body.api_base, body.options or {}, contract)
        version = ModelConfigVersion(
            id=str(uuid.uuid4()), model_config_id=config.id, version_no=1,
            provider=body.provider, api_base=body.api_base, options=body.options or {},
            behavior_hash=digest, model_contract=contract, conservative_input_limit=None,
            created_by=current_user.id,
        )
        db.add(version)
        db.flush()
        db.execute(text(
            "INSERT INTO model_credentials "
            "(id, model_config_id, secret_encrypted, status, secret_revision, created_at) "
            "VALUES (:id, :config, :secret, 'active', 1, now())"
        ), {"id": str(uuid.uuid4()), "config": config.id, "secret": config.api_key_encrypted})
        db.execute(text(
            "UPDATE model_configs SET status = 'active', active_version_id = :vid WHERE id = :id"
        ), {"vid": version.id, "id": config.id})
    db.commit()
    db.refresh(config)
    return {"data": _serialize_config(config, db)}


@router.get("/{model_id}")
def get_model(model_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    return {"data": _serialize_config(c, db)}


@router.put("/{model_id}", status_code=201)
def update_model(model_id: str, body: ModelConfigUpdate, db: Session = Depends(get_db),
                 _=Depends(require_editor), response: Response = None):
    """Legacy compatibility alias for `POST /{model_id}/versions` (201 +
    deprecation header).  Name-only updates the identity; behavior changes
    create immutable version N+1; secret-only changes rotate the credential."""
    if response is None:  # pragma: no cover — FastAPI always injects
        response = Response()
    response.headers["Deprecation"] = "true"
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    from app.services.model_version import versioning_schema_present
    if not versioning_schema_present(db):
        # pre-0004 schema: legacy inline update (compat alias semantics)
        if body.name is not None:
            c.name = body.name
        if body.provider is not None:
            c.provider = body.provider
        if body.api_key is not None:
            c.api_key_encrypted = encrypt(body.api_key)
        if body.api_base is not None:
            c.api_base = body.api_base
        if body.models is not None:
            c.models = body.models
        if body.options is not None:
            c.options = body.options
        db.commit()
        db.refresh(c)
        return {"data": _serialize_config(c, db)}
    if (c.config_type or "llm") != "llm":
        raise HTTPException(422, detail="MODEL_VERSIONING_LLM_ONLY")
    if body.name is not None and body.name != c.name:
        c.name = body.name
        db.flush()
    touches_behavior = any(
        v is not None for v in (body.provider, body.api_base, body.options, body.models)
    )
    active = None
    if touches_behavior:
        try:
            active = select_active_version(db, model_id)
        except ModelVersionUnavailable:
            raise HTTPException(409, detail="MODEL_VERSION_UNAVAILABLE")
    active_models = [entry.get("provider_model_revision") for entry in (active.model_contract or [])] if active else []
    models_changed = body.models is not None and body.models != active_models
    has_behavior = touches_behavior and (
        models_changed
        or (body.provider is not None and body.provider != active.provider)
        or (body.api_base is not None and body.api_base != active.api_base)
        or (body.options is not None and body.options != active.options)
    )
    if has_behavior:
        contract = legacy_contract_for(body.models) if models_changed else []
        try:
            version = create_next_version(
                db, model_id,
                base_version=None,
                provider=body.provider,
                api_base=body.api_base if body.api_base is not None else active.api_base,
                options=body.options if body.options is not None else active.options,
                model_contract=contract,
                credential_binding=body.api_key,
                # this endpoint only ever supplies a legacy_contract_for()
                # fallback (ModelConfigUpdate has no verified-contract field)
                # — same trust class create_model()'s bootstrap already
                # accepts without validation
                validate_contract=False,
            )
        except ModelRevisionConflict as exc:
            raise HTTPException(409, detail=str(exc))
        except ModelContractInvalid as exc:
            raise HTTPException(422, detail=str(exc))
        except ModelVersionUnavailable as exc:
            raise HTTPException(409, detail=str(exc))
        # `ModelConfigOut` (the list/detail view) reads these off the
        # identity row directly, not off the new version — keep them in
        # sync or an admin's edit never appears to have taken effect.
        if body.provider is not None:
            c.provider = body.provider
        if body.api_base is not None:
            c.api_base = body.api_base
        if body.models is not None:
            c.models = body.models
        if body.options is not None:
            c.options = body.options
        db.commit()
        c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
        return {"data": _serialize_config(c, db)}
    if body.api_key is not None:
        rotate_credential(db, model_id, body.api_key)
    db.commit()
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    return {"data": _serialize_config(c, db)}


@router.post("/{model_id}/versions", status_code=201)
def create_model_version(model_id: str, body: ModelVersionCreate, db: Session = Depends(get_db), _=Depends(require_editor)):
    from app.services.model_version import versioning_schema_present
    if not versioning_schema_present(db):
        raise HTTPException(422, detail="MODEL_VERSIONING_UNAVAILABLE")
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    if (c.config_type or "llm") != "llm":
        raise HTTPException(422, detail="MODEL_VERSIONING_LLM_ONLY")
    try:
        active = select_active_version(db, model_id)
        contract = [entry.model_dump() for entry in (body.model_contract or [])]
        is_legacy_fallback = not contract and body.models is not None
        if is_legacy_fallback:
            contract = legacy_contract_for(body.models)
        version = create_next_version(
            db, model_id,
            base_version=body.base_version,
            provider=active.provider,
            api_base=body.api_base if body.api_base is not None else active.api_base,
            options=body.options if body.options is not None else active.options,
            model_contract=contract,
            credential_binding=body.credential_binding,
            # only an explicit body.model_contract asserts verified data —
            # the legacy_contract_for() fallback is never validated (same
            # trust class create_model()'s bootstrap already accepts)
            validate_contract=not is_legacy_fallback,
        )
        # `ModelConfigOut` (the list/detail view) reads these off the
        # identity row directly, not off the new version — keep them in sync.
        if body.api_base is not None:
            c.api_base = body.api_base
        if body.models is not None:
            c.models = body.models
        if body.options is not None:
            c.options = body.options
        db.commit()
    except ModelRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc))
    except ModelContractInvalid as exc:
        raise HTTPException(422, detail=str(exc))
    except ModelVersionUnavailable as exc:
        raise HTTPException(409, detail=str(exc))
    return {"data": ModelVersionOut.model_validate(version).model_dump()}


@router.get("/{model_id}/versions")
def list_model_versions(model_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    versions = db.query(ModelConfigVersion).filter(
        ModelConfigVersion.model_config_id == model_id
    ).order_by(ModelConfigVersion.version_no.asc()).all()
    return {"data": [ModelVersionOut.model_validate(v).model_dump() for v in versions]}


@router.get("/{model_id}/versions/{version_id}")
def get_model_version(model_id: str, version_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    v = db.query(ModelConfigVersion).filter(
        ModelConfigVersion.id == version_id, ModelConfigVersion.model_config_id == model_id
    ).first()
    if not v:
        raise HTTPException(404, "Not found")
    return {"data": ModelVersionOut.model_validate(v).model_dump()}


@router.get("/{model_id}/credentials")
def list_model_credentials(model_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    rows = db.execute(text(
        "SELECT status, secret_revision, rotated_at, revoked_at, created_at "
        "FROM model_credentials WHERE model_config_id = :id ORDER BY secret_revision DESC"
    ), {"id": model_id}).mappings().all()
    return {"data": [ModelCredentialOut(**{k: row[k] for k in ("status", "secret_revision", "rotated_at", "revoked_at", "created_at")}).model_dump() for row in rows]}


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    # Block only on a REAL dependent — an Agent version pinned to one of this
    # model's versions, or a prompt-generation provenance row — not merely on
    # the model's own version-1 row that create_model() always bootstraps.
    # Checking "any model_config_versions row exists" made every model saved
    # through the normal create flow permanently undeletable.
    referenced = db.execute(text(
        "SELECT count(*) FROM model_config_versions v WHERE v.model_config_id = :id AND ("
        "EXISTS (SELECT 1 FROM agent_versions av WHERE av.default_model_config_version_id = v.id) "
        "OR EXISTS (SELECT 1 FROM prompt_generations pg WHERE pg.model_config_version_id = v.id))"
    ), {"id": model_id}).scalar_one()
    if referenced > 0:
        raise HTTPException(409, detail="MODEL_REFERENCED")
    db.query(ExtractionTask).filter(ExtractionTask.model_id == model_id).update(
        {ExtractionTask.model_id: None}, synchronize_session=False
    )
    # model_config_versions/model_credentials/model_migration_findings all
    # RESTRICT-reference model_configs.id, so they must be cleared first —
    # and model_configs.active_version_id RESTRICT-references the version
    # row itself (the reverse direction), so that pointer must be nulled
    # before the version row can go.
    db.execute(text("UPDATE model_configs SET active_version_id = NULL WHERE id = :id"), {"id": model_id})
    db.execute(text("DELETE FROM model_credentials WHERE model_config_id = :id"), {"id": model_id})
    db.execute(text("DELETE FROM model_migration_findings WHERE model_config_id = :id"), {"id": model_id})
    db.execute(text("DELETE FROM model_config_versions WHERE model_config_id = :id"), {"id": model_id})
    db.delete(c)
    db.commit()


@router.post("/{model_id}/test")
def test_model(model_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    c = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    try:
        if (c.config_type or "llm") == "ocr":
            if c.provider == "easyocr":
                import os
                enabled = os.getenv("ENABLE_OCR", "").lower() in ("1", "true", "yes") or bool((c.options or {}).get("enabled"))
                if not enabled:
                    return {"data": {"ok": False, "response": "EasyOCR is configured but disabled. Enable it in OCR model config or set ENABLE_OCR=1."}}
                import easyocr  # noqa: F401
                return {"data": {"ok": True, "response": "EasyOCR import ok"}}
            if c.provider == "paddleocr":
                import os
                enabled = (
                    os.getenv("ENABLE_OCR", "").lower() in ("1", "true", "yes")
                    or os.getenv("ENABLE_PADDLEOCR", "").lower() in ("1", "true", "yes")
                    or bool((c.options or {}).get("enabled"))
                )
                if not enabled:
                    return {"data": {"ok": False, "response": "PaddleOCR is configured but disabled. Enable it in OCR model config or set ENABLE_OCR=1."}}
                from paddleocr import PaddleOCR  # noqa: F401
                return {"data": {"ok": True, "response": "PaddleOCR import ok"}}
            if c.provider == "external_api":
                if not c.api_base:
                    raise HTTPException(400, "External OCR requires API Base")
                return {"data": {"ok": True, "response": "External OCR endpoint configured"}}
            return {"data": {"ok": True, "response": f"OCR provider configured: {c.provider}"}}

        if (c.config_type or "llm") != "llm":
            return {"data": {"ok": True, "response": f"Config type configured: {c.config_type}"}}

        from app.services.model_callers.extraction import resolve_llm_caller, ModelVersionUnavailableError
        try:
            call_kwargs = resolve_llm_caller(db, model_id)
        except ModelVersionUnavailableError:
            raise HTTPException(409, detail="MODEL_VERSION_UNAVAILABLE")
        api_key = call_kwargs["api_key"]
        if c.provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            model = call_kwargs["model"]
            resp = client.messages.create(model=model, max_tokens=10, messages=[{"role": "user", "content": "ping"}])
            return {"data": {"ok": True, "response": resp.content[0].text}}
        else:
            import openai
            kwargs = {"api_key": api_key}
            if call_kwargs["api_base"]:
                kwargs["base_url"] = call_kwargs["api_base"]
            client = openai.OpenAI(**kwargs)
            model = call_kwargs["model"]
            resp = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=10)
            return {"data": {"ok": True, "response": resp.choices[0].message.content}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {e}")


admin_router = APIRouter(
    prefix="/api/v1/admin/model-migration-remediations",
    tags=["admin-model-remediation"],
)


def _finding_row(db: Session, finding_id: str):
    return db.execute(text(
        "SELECT f.id, f.model_config_id, f.code, f.field, f.reason, mc.updated_at "
        "FROM model_migration_findings f "
        "JOIN model_configs mc ON mc.id = f.model_config_id "
        "WHERE f.id = :id"
    ), {"id": finding_id}).mappings().one_or_none()


@admin_router.get("")
def list_model_remediations(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.execute(text(
        "SELECT f.id, f.model_config_id, mc.name, f.code, f.field, f.reason, mc.updated_at "
        "FROM model_configs mc "
        "JOIN model_migration_findings f ON f.model_config_id = mc.id "
        "WHERE mc.status = 'migration_blocked' "
        "ORDER BY f.created_at, mc.id"
    )).mappings().all()
    items = [{
        "id": r["id"],
        "model_config_id": r["model_config_id"],
        "name": r["name"],
        "code": r["code"],
        "field": r["field"],
        "reason": r["reason"],
        "base_revision": r["updated_at"].isoformat() if r["updated_at"] else None,
    } for r in rows]
    return {"data": {"items": items, "next_cursor": None, "has_more": False}}


@admin_router.get("/{finding_id}")
def get_model_remediation(finding_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    row = _finding_row(db, finding_id)
    if not row:
        raise HTTPException(404, "Not found")
    return {"data": {
        "id": row["id"],
        "model_config_id": row["model_config_id"],
        "code": row["code"],
        "field": row["field"],
        "reason": row["reason"],
        "base_revision": row["updated_at"].isoformat() if row["updated_at"] else None,
    }}


@admin_router.post("/{finding_id}/remediate", status_code=201)
def remediate_model(finding_id: str, body: RemediateModelMigrationRequest,
                    db: Session = Depends(get_db), _=Depends(require_admin)):
    row = _finding_row(db, finding_id)
    if not row:
        raise HTTPException(404, "Not found")
    try:
        version = remediate_blocked_identity(
            db, row["model_config_id"], base_revision=body.base_revision,
            provider=body.provider, api_base=body.api_base, options=body.options or {},
            model_contract=[e.model_dump() for e in body.model_contract],
            credential_binding=body.credential_binding,
        )
        db.commit()
    except ModelRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc))
    except ModelContractInvalid as exc:
        raise HTTPException(422, detail=str(exc))
    except ModelVersionUnavailable as exc:
        raise HTTPException(404, detail=str(exc))
    return {"data": ModelVersionOut.model_validate(version).model_dump()}


@admin_router.post("/{finding_id}/archive")
def archive_model(finding_id: str, body: ArchiveModelMigrationRequest,
                  db: Session = Depends(get_db), _=Depends(require_admin)):
    row = _finding_row(db, finding_id)
    if not row:
        raise HTTPException(404, "Not found")
    try:
        archive_blocked_identity(
            db, row["model_config_id"], base_revision=body.base_revision, reason=body.reason,
        )
        db.commit()
    except ModelRevisionConflict as exc:
        raise HTTPException(409, detail=str(exc))
    except ModelVersionUnavailable as exc:
        raise HTTPException(404, detail=str(exc))
    return {"data": {"id": finding_id, "model_config_id": row["model_config_id"], "status": "archived"}}
