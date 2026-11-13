"""Pipeline 执行 Celery 任务 — 支持 DAG 编译 + 节点状态追踪"""
from __future__ import annotations
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Mapping, Sequence
import uuid

logger = logging.getLogger(__name__)

from app.services.connection.config import build_connector_config, safe_connection_error
from app.services.connection.registry import get_connector


def get_celery_app():
    try:
        from app.tasks.extraction import celery_app
        return celery_app
    except Exception:
        return None


celery_app = get_celery_app()


def _init_node_status(definition: dict | None) -> dict[str, str]:
    """从 definition 中提取所有节点 ID，初始化为 'idle'"""
    if not definition:
        return {}
    nodes = definition.get("nodes", [])
    return {n["id"]: "idle" for n in nodes}


def _compute_quality_score(rows: list[dict], route: str, meta: dict) -> float:
    if not rows:
        return 0.0
    if route == "C":
        meta_fields = {"markdown_text", "filename", "source_file", "source_dataset_id",
                       "extraction_strategy", "extraction_method", "structured_extraction_ok",
                       "structured_extraction_error"}
        meaningful_fields = [k for k in rows[0].keys() if k not in meta_fields]
        total_fields = len(meaningful_fields) or 1
        filled = sum(1 for row in rows for k in meaningful_fields if row.get(k))
        completeness = filled / (len(rows) * total_fields) if total_fields > 0 else 0
        rule_bonus = min(0.2, int(rows[0].get("rule_count", 0)) * 0.02)
        return min(1.0, completeness + rule_bonus)
    rows_before = meta.get("rows_before", len(rows)) or len(rows)
    rows_after = meta.get("rows_after", len(rows)) or len(rows)
    retention = rows_after / rows_before if rows_before > 0 else 1.0
    total_cells = sum(len(r) for r in rows) or 1
    filled_cells = sum(1 for r in rows for v in r.values() if v is not None and str(v).strip() != "")
    fill_rate = filled_cells / total_cells
    return round(retention * 0.4 + fill_rate * 0.6, 3)


def _route_for_kind(kind: str | None, default_route: str | None = None) -> str:
    if default_route in ("A", "B", "C"):
        return default_route
    if kind == "semi":
        return "B"
    if kind == "unstructured":
        return "C"
    return "A"


def _transform_nodes(definition: dict | None) -> list[dict]:
    if not definition:
        return []
    return [n for n in definition.get("nodes") or [] if n.get("type") == "transform"]


def _route_from_transform_config(config: dict | None) -> str | None:
    path = (config or {}).get("path")
    if path == "structured":
        return "A"
    if path == "semi_structured":
        return "B"
    if path == "unstructured":
        return "C"
    if path == "wide_table":
        return "A"
    return None


def _merge_dict(base: dict, overlay: dict) -> dict:
    result = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _spec_from_transform_config(config: dict | None) -> dict:
    config = config or {}
    spec: dict = {}
    steps = config.get("steps") or []

    if config.get("engine"):
        spec["engine"] = config.get("engine")
    if config.get("path"):
        spec["path"] = config.get("path")

    for step in steps:
        op = step.get("op") if isinstance(step, dict) else None
        params = step.get("params") if isinstance(step, dict) else {}
        params = params or {}

        if op in ("parse_json", "flatten_json", "explode_array"):
            spec["format"] = "json"
            flatten = dict(spec.get("json_flatten") or {})
            if op == "explode_array":
                flatten["array_explode"] = True
            flatten.update(params)
            spec["json_flatten"] = flatten
        elif op == "parse_xml":
            spec["format"] = "xml"
        elif op in ("detect_wide_table", "suggest_split", "apply_split"):
            wide = dict(spec.get("wide_table_split") or {})
            wide["enabled"] = True
            if op in ("detect_wide_table", "suggest_split"):
                wide["suggest_only"] = True
            if op == "apply_split":
                wide["suggest_only"] = False
            wide.update(params)
            spec["wide_table_split"] = wide
        elif op in ("drop_duplicates", "drop_nulls", "fill_nulls", "normalize_dates"):
            cleansing = dict(spec.get("cleansing") or {})
            if op == "drop_duplicates":
                cleansing["deduplicate"] = True
            elif op == "drop_nulls":
                cleansing["null_strategy"] = "drop"
            elif op == "fill_nulls":
                cleansing["null_strategy"] = params.get("strategy", "fill_empty")
            elif op == "normalize_dates":
                cleansing["normalize_dates"] = True
            cleansing.update({k: v for k, v in params.items() if k != "strategy"})
            spec["cleansing"] = cleansing
        elif op == "document_to_markdown":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = params.get("strategy", doc.get("strategy", "markitdown"))
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "ocr_extract":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = "ocr"
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "vlm_extract":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = "vlm"
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "llm_structurize":
            extract = dict(spec.get("md_to_structured") or {})
            extract["auto_extract"] = True
            extract.update(params)
            spec["md_to_structured"] = extract
        elif op == "llm_enrich":
            # 方案三：规则提取后 LLM 语义增强，不影响结构确定性
            extract = dict(spec.get("md_to_structured") or {})
            extract["llm_enrich"] = True
            if params.get("fields"):
                extract["enrich_fields"] = params["fields"]
            spec["md_to_structured"] = extract

    if config.get("path") == "wide_table":
        wide = dict(spec.get("wide_table_split") or {})
        wide.setdefault("enabled", True)
        wide.setdefault("suggest_only", False)
        spec["wide_table_split"] = wide
    return spec


def _pipeline_runtime_config(pl) -> tuple[str | None, dict]:
    transforms = _transform_nodes(pl.definition)
    route = None
    spec = dict(pl.spec or {})
    for node in transforms:
        cfg = node.get("config") or {}
        route = route or _route_from_transform_config(cfg)
        spec = _merge_dict(spec, _spec_from_transform_config(cfg))
    return route, spec


def _source_runtime_route(source: dict, transform_route: str | None, default_route: str | None) -> str:
    return transform_route or source.get("route") or _route_for_kind(source.get("kind"), default_route)


def _find_dataset_for_file(db, filename: str):
    from app.models.v2.dataset import Dataset, DatasetVersion

    stem = Path(filename).stem
    candidates = db.query(Dataset).filter(
        Dataset.name == stem
    ).order_by(Dataset.created_at.desc()).limit(20).all()
    for candidate in candidates:
        ver = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == candidate.id
        ).order_by(DatasetVersion.version_no.desc()).first()
        if ver and ((ver.rowcount or 0) > 0 or ver.storage_uri):
            return candidate
    return candidates[0] if candidates else None


def _collect_sources(db, pl) -> list[dict]:
    from app.models.v2.connection import Connection
    from app.models.v2.dataset import Dataset

    sources: list[dict] = []
    definition = pl.definition or {}
    for node in definition.get("nodes") or []:
        if node.get("type") != "connector":
            continue
        config = node.get("config") or {}
        source_type = str(config.get("source_type") or "").lower()
        if source_type in {"mysql", "postgres", "postgresql"}:
            connection_id = str(config.get("connection_id") or "")
            resource = str(config.get("resource") or config.get("table") or "")
            if not connection_id:
                raise ValueError("Database connector must select a saved connection")
            if not resource:
                raise ValueError("Database connector must select a table")
            connection = db.get(Connection, connection_id)
            if connection is None:
                raise ValueError(f"Saved connection not found: {connection_id}")
            sources.append({
                "connection_id": connection.id,
                "resource": resource,
                "filename": resource,
                "route": "A",
                "kind": "structured",
                "dataset_id": None,
            })
            continue

        for file_info in config.get("files", []) or []:
            filename = file_info.get("name") or file_info.get("filename") or ""
            dataset_id = file_info.get("dataset_id")
            ds = db.query(Dataset).filter(Dataset.id == dataset_id).first() if dataset_id else None
            if not ds and filename:
                ds = _find_dataset_for_file(db, filename)
            if ds:
                sources.append({
                    "dataset_id": ds.id,
                    "filename": filename or ds.name,
                    "route": _route_for_kind(ds.kind, None),
                    "kind": ds.kind,
                })

    if not sources and pl.source_dataset_id:
        ds = db.query(Dataset).filter(Dataset.id == pl.source_dataset_id).first()
        if ds:
            sources.append({
                "dataset_id": ds.id,
                "filename": ds.name,
                "route": _route_for_kind(ds.kind, pl.route),
                "kind": ds.kind,
            })

    # Preserve order while removing duplicate datasets.
    seen: set[str] = set()
    unique_sources = []
    for source in sources:
        key = source.get("dataset_id") or (
            f"connection:{source.get('connection_id')}:{source.get('resource')}"
        )
        if key in seen:
            continue
        seen.add(key)
        unique_sources.append(source)
    return unique_sources


def _existing_database_dataset(db, connection_id: str, resource: str):
    from app.models.v2.dataset import Dataset

    return db.query(Dataset).filter(
        Dataset.source_connection_id == connection_id,
        Dataset.name == resource,
    ).order_by(Dataset.created_at.desc()).first()


def _pinned_database_version(db, dataset_id: str, input_dataset_version_ids) -> object | None:
    if not input_dataset_version_ids:
        return None
    from app.models.v2.dataset import DatasetVersion

    for version_id in input_dataset_version_ids:
        version = db.get(DatasetVersion, version_id)
        if version is not None and version.dataset_id == dataset_id:
            return version
    return None


def _resolve_pinned_input_versions(db, sources: list[dict], input_dataset_version_ids) -> dict[int, object]:
    """Resolve one immutable input version for every source before any pull.

    A refresh-triggered pipeline must not silently fill a missing source pin
    from a current/latest dataset version.  SQL sources may not have a
    materialized Dataset until their first unpinned run, so their candidate
    Dataset rows are resolved by the saved connection/resource identity.
    """
    if not input_dataset_version_ids:
        return {}

    from app.models.v2.dataset import Dataset, DatasetVersion

    requested_ids = list(input_dataset_version_ids)
    versions = db.query(DatasetVersion).filter(DatasetVersion.id.in_(set(requested_ids))).all()
    versions_by_id = {version.id: version for version in versions}
    resolved: dict[int, object] = {}

    for source in sources:
        dataset_ids: set[str] = set()
        if source.get("dataset_id"):
            dataset_ids.add(source["dataset_id"])
        elif source.get("connection_id"):
            datasets = db.query(Dataset).filter(
                Dataset.source_connection_id == source["connection_id"],
                Dataset.name == source["resource"],
            ).all()
            dataset_ids.update(dataset.id for dataset in datasets)

        matches = [
            versions_by_id[version_id]
            for version_id in requested_ids
            if version_id in versions_by_id
            and versions_by_id[version_id].dataset_id in dataset_ids
        ]
        if len(matches) != 1:
            source_name = source.get("resource") or source.get("filename") or "unknown"
            raise ValueError(
                "PINNED_INPUTS_INCOMPLETE: source "
                f"{source_name!r} requires exactly one pinned dataset version"
            )
        resolved[id(source)] = matches[0]

    return resolved


def _snapshot_database_source(
    db, svc, source: dict, input_dataset_version_ids, selected_version=None,
) -> object:
    """Materialize a selected SQL table as an immutable DatasetVersion.

    A refresh-triggered run can provide an exact pinned version; in that case
    the live table is never read again.  Manual runs without a pin read the
    selected resource once and persist a new snapshot before Route A loads it.
    """
    from app.models.v2.connection import Connection
    from app.models.v2.dataset import Dataset

    connection = db.get(Connection, source["connection_id"])
    if connection is None:
        raise ValueError(f"Saved connection not found: {source['connection_id']}")
    dataset = _existing_database_dataset(db, connection.id, source["resource"])
    if selected_version is not None:
        # A pinned SQL version is already immutable.  Reuse it directly and
        # recover the Dataset identity without touching the live connector.
        if dataset is None:
            dataset = db.get(Dataset, selected_version.dataset_id)
        if (
            dataset is None
            or dataset.id != selected_version.dataset_id
            or dataset.source_connection_id != connection.id
            or dataset.name != source["resource"]
        ):
            raise ValueError(
                "PINNED_INPUTS_INCOMPLETE: pinned version does not belong to "
                f"{connection.id}/{source['resource']}"
            )
        source["dataset_id"] = dataset.id
        return selected_version
    if dataset is not None:
        pinned = _pinned_database_version(db, dataset.id, input_dataset_version_ids)
        if pinned is not None:
            source["dataset_id"] = dataset.id
            return pinned

    config = build_connector_config(
        connection.kind, connection.config, resource=source["resource"],
    )
    # A pipeline table selector is authoritative.  Do not allow an optional
    # saved query to redirect it to an arbitrary statement.
    config.pop("query", None)
    connector = get_connector(connection.kind, config)
    rows = connector.pull_full(source["resource"])
    if isinstance(rows, Mapping):
        rows = [dict(rows)]
    else:
        rows = [dict(row) for row in (rows or [])]

    if dataset is None:
        dataset = svc.create_dataset(
            name=source["resource"], kind="structured", connection_id=connection.id,
        )
    version = svc.create_version(
        dataset.id, _safe_csv_bytes(rows), rowcount=len(rows),
    )
    source["dataset_id"] = dataset.id
    return version


def _load_source_rows(
    db,
    svc,
    source: dict,
    limit: int = 10000,
    input_dataset_version_ids=None,
    selected_version=None,
) -> list[dict]:
    if selected_version is None:
        from app.services.v2.incremental.polling import select_pinned_dataset_version

        selected_version = select_pinned_dataset_version(
            db,
            dataset_id=source["dataset_id"],
            input_dataset_version_ids=input_dataset_version_ids,
        )
    version = selected_version
    if source["route"] == "C":
        if not version or not version.storage_uri:
            return []
        raw = svc._storage.get_object(version.storage_uri)
        return [{
            "filename": source["filename"],
            "content": raw,
            "storage_uri": version.storage_uri,
            "source_dataset_id": source["dataset_id"],
        }]
    if version is None:
        return []
    # A database snapshot is already a bounded, immutable input selected by
    # the pipeline.  Loading only the preview cap would silently drop rows
    # from the pipeline, so request the complete version for DB sources while
    # retaining the existing preview cap for file sources.
    preview_limit = None if source.get("connection_id") else limit
    return svc.preview(
        source["dataset_id"], version.version_no,
        limit=preview_limit, version_id=version.id,
    )


def _execute_route(route: str, ctx, data: list[dict]) -> tuple[list[dict], object]:
    from app.services.v2.pipeline.engine import execute_route_a, execute_route_b, execute_route_c

    if route == "B":
        return execute_route_b(ctx, data)
    if route == "C":
        return execute_route_c(ctx, data)
    return execute_route_a(ctx, data)


def _safe_csv_bytes(data: list[dict]) -> bytes:
    import csv
    import io
    import json as _json

    def _safe_str(v) -> str:
        if v is None:
            return ""
        if isinstance(v, bytes):
            return f"<{len(v)} bytes>"
        if isinstance(v, (dict, list)):
            return _json.dumps(v, ensure_ascii=False)
        return str(v)

    if not data:
        return b""

    all_keys: list[str] = []
    seen_keys: set[str] = set()
    for row in data:
        for key in row.keys():
            if key == "content":
                continue
            if key not in seen_keys:
                all_keys.append(key)
                seen_keys.add(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_keys, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in data:
        writer.writerow({k: _safe_str(row.get(k)) for k in all_keys})
    return buf.getvalue().encode("utf-8")


def _save_curated_dataset(db, svc, pl, source: dict, data: list[dict], ctx, multi_source: bool, table_name: str | None = None) -> dict:
    stem = Path(source["filename"]).stem
    name_parts = [pl.name]
    if multi_source:
        name_parts.append(stem)
    if table_name:
        name_parts.append(table_name)
    name_parts.append("curated")
    ds_name = " ".join(name_parts)
    curated_ds = svc.create_dataset(name=ds_name, kind="curated")
    curated_version = svc.create_version(curated_ds.id, _safe_csv_bytes(data), rowcount=len(data))

    if data:
        try:
            # 赋新 dict, 原地修改 JSON 列不会被 SQLAlchemy 跟踪
            schema = dict(curated_ds.schema_json or {})
            schema["quality_score"] = _compute_quality_score(data, source["route"], ctx.meta)
            schema["columns"] = [k for k in data[0].keys() if k != "content"]
            schema["route"] = source["route"]
            schema["source_dataset_id"] = source["dataset_id"]
            if table_name:
                schema["transform_output_table"] = table_name
            curated_ds.schema_json = schema
            db.commit()
        except Exception:
            pass

    return {
        "source_dataset_id": source["dataset_id"],
        "source_file": source["filename"],
        "route": source["route"],
        "table_name": table_name,
        "curated_dataset_id": curated_ds.id,
        "curated_dataset_version_id": curated_version.id,
        "rows_in": ctx.rows_in,
        "rows_out": len(data),
        "meta": ctx.meta,
    }


def _save_curated_outputs(db, svc, pl, source: dict, data: list[dict], ctx, multi_source: bool) -> list[dict]:
    split_tables = ctx.meta.get("split_tables")
    if isinstance(split_tables, dict) and split_tables:
        outputs = []
        for table_name, rows in split_tables.items():
            outputs.append(_save_curated_dataset(
                db, svc, pl, source, rows or [], ctx, multi_source=True, table_name=str(table_name)
            ))
        return outputs
    return [_save_curated_dataset(db, svc, pl, source, data, ctx, multi_source)]


def _set_pipeline_status_if_latest(db, pipeline, run, status: str) -> bool:
    """Update the pipeline summary only for the newest deterministic run."""
    from app.models.v2.pipeline import PipelineRun

    latest = db.query(PipelineRun).filter(
        PipelineRun.pipeline_id == run.pipeline_id,
    ).order_by(
        PipelineRun.created_at.desc(), PipelineRun.id.desc(),
    ).first()
    if latest is None or latest.id != run.id:
        return False
    pipeline.status = status
    return True


def pipeline_run_task(
    pipeline_id: str, run_id: str,
    input_dataset_version_ids: Sequence[str] | None = None,
):
    """Pipeline 执行任务 — 支持 DAG 编译 + 节点状态追踪"""
    from app.database import SessionLocal
    from app.models.v2.pipeline import Pipeline, PipelineRun, PipelineRunInput
    from app.services.v2.pipeline.base import PipelineContext
    from app.services.v2.pipeline.dag_compiler import compile_definition
    from app.services.v2.dataset_service import DatasetService
    from app.services.v2.incremental.polling import select_pinned_dataset_version

    db = SessionLocal()
    run = None
    pl = None
    try:
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
        if not run:
            return

        pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pl:
            run.status = "failed"
            run.error_log = "Pipeline not found"
            db.commit()
            return

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        _set_pipeline_status_if_latest(db, pl, run, "running")
        db.commit()

        # ── DAG 编译 ──────────────────────────────────────────────
        definition = pl.definition
        plan = compile_definition(definition)
        node_status = _init_node_status(definition)

        def set_node_status(nid: str, status: str):
            if nid in node_status:
                node_status[nid] = status
                # 每步更新持久化到 run; 必须赋新 dict, 原地修改 JSON 列不会被 SQLAlchemy 跟踪
                run.stats = {**(run.stats or {}), "node_status": dict(node_status)}
                db.commit()

        svc = DatasetService(db)
        sources = _collect_sources(db, pl)
        if not sources:
            raise ValueError("Pipeline has no source datasets")

        # Refresh polling pins every input version before dispatch.  Keep the
        # durable PipelineRunInput order when the worker is replayed without
        # an explicit argument; otherwise the task payload carries the exact
        # versions selected by the refresh outcome.
        if input_dataset_version_ids is None:
            input_dataset_version_ids = [value for value in db.query(PipelineRunInput.dataset_version_id).filter(
                PipelineRunInput.pipeline_run_id == run.id,
            ).order_by(PipelineRunInput.input_ordinal).all()]
            input_dataset_version_ids = [value[0] for value in input_dataset_version_ids]
        else:
            input_dataset_version_ids = list(input_dataset_version_ids)
        lineage_version_ids = list(input_dataset_version_ids)
        pinned_versions = _resolve_pinned_input_versions(
            db, sources, input_dataset_version_ids,
        )
        if input_dataset_version_ids:
            run.dataset_version_id = input_dataset_version_ids[0]
            db.commit()

        transform_route, runtime_spec = _pipeline_runtime_config(pl)

        outputs = []
        multi_source = len(sources) > 1
        for source in sources:
            pinned_version = pinned_versions.get(id(source))
            if source.get("connection_id"):
                source_version = _snapshot_database_source(
                    db, svc, source, input_dataset_version_ids,
                    selected_version=pinned_version,
                )
            else:
                source_version = pinned_version
            # SQL source identity is only known after its first snapshot.
            # A multi-source pipeline cannot be represented by this one-source
            # refresh binding; never silently bind it to the first input.
            if not multi_source and not pl.source_dataset_id and source.get("dataset_id"):
                pl.source_dataset_id = source["dataset_id"]
                db.commit()
            source["route"] = _source_runtime_route(source, transform_route, pl.route)
            source_version_ids = [source_version.id] if pinned_version is not None else []
            if source_version is None:
                source_version = select_pinned_dataset_version(
                    db,
                    dataset_id=source["dataset_id"],
                    input_dataset_version_ids=source_version_ids or None,
                )
            data = _load_source_rows(
                db, svc, source,
                input_dataset_version_ids=source_version_ids or None,
                selected_version=source_version,
            )
            if source_version is not None and source_version.id not in lineage_version_ids:
                lineage_version_ids.append(source_version.id)
            ctx = PipelineContext(
                dataset_id=source["dataset_id"],
                version_no=source_version.version_no if source_version is not None else None,
                dataset_version_id=source_version.id if source_version is not None else None,
                route=source["route"],
                spec=runtime_spec,
            )
            if source["route"] == "C":
                ctx.spec = dict(ctx.spec or {})
                # 默认使用规则提取保证可重复性。只有当 pipeline spec 中已明确写入
                # md_to_structured.auto_extract=true 时才调用 LLM（非确定性）。
                existing_md_spec = ctx.spec.get("md_to_structured") or {}
                if existing_md_spec.get("auto_extract"):
                    # Pipeline spec 显式请求 LLM 提取：检查模型是否可用
                    from app.services.model_config_selector import select_llm_model_config
                    try:
                        _has_llm = bool(select_llm_model_config(
                            purpose_tags=("结构化提取", "结构化抽取"), allow_vlm=False))
                    except Exception:
                        _has_llm = False
                    if not _has_llm:
                        existing_md_spec = {k: v for k, v in existing_md_spec.items() if k != "auto_extract"}
                        existing_md_spec["rule_based"] = True
                    ctx.spec["md_to_structured"] = existing_md_spec
                else:
                    # 默认规则提取（确定性）
                    ctx.spec["md_to_structured"] = {"rule_based": True, **existing_md_spec}
            ctx.rows_in = len(data)
            data, ctx = _execute_route(source["route"], ctx, data)
            ctx.rows_out = len(data)
            outputs.extend(_save_curated_outputs(db, svc, pl, source, data, ctx, multi_source))

        for nid in node_status:
            set_node_status(nid, "success")

        curated_ids = [o["curated_dataset_id"] for o in outputs]
        pl.target_curated_ids = curated_ids
        if len({s["route"] for s in sources}) == 1:
            pl.route = sources[0]["route"]
        else:
            pl.route = pl.route or sources[0]["route"] or "A"
        if outputs:
            run.dataset_version_id = outputs[0]["curated_dataset_version_id"]
        existing_lineage = {
            value[0] for value in db.query(PipelineRunInput.dataset_version_id).filter(
                PipelineRunInput.pipeline_run_id == run.id,
            ).all()
        }
        next_input_ordinal = db.query(PipelineRunInput.input_ordinal).filter(
            PipelineRunInput.pipeline_run_id == run.id,
        ).order_by(PipelineRunInput.input_ordinal.desc()).first()
        next_input_ordinal = (next_input_ordinal[0] + 1) if next_input_ordinal else 0
        for version_id in lineage_version_ids:
            if version_id in existing_lineage:
                continue
            db.add(PipelineRunInput(
                id=str(uuid.uuid4()),
                pipeline_run_id=run.id,
                dataset_version_id=version_id,
                input_ordinal=next_input_ordinal,
            ))
            next_input_ordinal += 1
        if run.dataset_version_id and run.dataset_version_id not in existing_lineage:
            db.add(PipelineRunInput(
                id=str(uuid.uuid4()), pipeline_run_id=run.id,
                dataset_version_id=run.dataset_version_id, input_ordinal=next_input_ordinal,
                provenance={"pipeline_output": True},
            ))
        db.commit()

        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        run.stats = {
            "rows_in": sum(o["rows_in"] for o in outputs),
            "rows_out": sum(o["rows_out"] for o in outputs),
            "meta": {"outputs": outputs},
            "node_status": node_status,
            "curated_dataset_id": curated_ids[0] if curated_ids else None,
            "curated_dataset_ids": curated_ids,
        }
        _set_pipeline_status_if_latest(db, pl, run, "published")
        db.commit()

    except Exception as e:
        detail = safe_connection_error(e)
        logger.error("Pipeline run failed: %s", detail)
        if run:
            run.status = "failed"
            run.error_log = detail
            run.finished_at = datetime.now(timezone.utc)
            stats = dict(run.stats or {})
            stats.setdefault("node_status", {})
            run.stats = stats
            _set_pipeline_status_if_latest(db, pl, run, "failed")
            db.commit()
    finally:
        db.close()


def _get_node_type(definition: dict | None, node_id: str) -> str:
    """从 definition 中获取节点类型"""
    if not definition:
        return ""
    for n in definition.get("nodes", []):
        if n.get("id") == node_id:
            return n.get("type", "")
    return ""


if celery_app:
    pipeline_run_task = celery_app.task(pipeline_run_task)
