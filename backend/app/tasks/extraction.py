from app.tasks.celery_app import celery_app

import logging
import re

logger = logging.getLogger(__name__)

# 确保 Celery worker 在 fork 前加载所有模型映射, 避免子进程缺少模型注册
from app.models import (  # noqa: E402, F401
    user, ontology, file, prompt, model_config,
    entity, logic as logic_model, action, relation, extraction_task, rules_config,
    entity_instance, entity_instance_relation,
)

# ── 概念/实例分离指令 — 追加到所有提取 Prompt 之后 ───────────────────────────
# entities 只放概念实体（对齐 Pipeline Mapping：一个类型一条 Entity），
# 文档中提到的具体命名对象放 instances，按 entity_type 挂到所属概念下。
CONCEPT_INSTANCE_DIRECTIVE = """

【概念与实例分离，务必遵守】
entities 数组只填"概念/类型"实体（如"供应商""借款人""贷款产品"），每个概念在全文中只出现一次，
不要为文档中提到的每个具体命名对象（如具体公司名、具体人名、具体订单号、具体条款标题）单独创建 entity。
文档中提到的具体命名实例，放入新增的 instances 数组，通过 entity_type 字段关联到其所属概念的 name_cn：
"instances": [{"entity_type": "所属概念的name_cn", "name_cn": "实例名称", "name_en": "可选", "properties": {"属性1": "值"}, "confidence": 0.9}]
relations 只在概念之间建立（如 供应商 -[supply]-> 产品），不要在具体实例之间建立关系。

如果两个具体命名实例之间存在文档明确写出的因果/治疗/相互作用等关系（如"2型糖尿病 导致 糖尿病肾病""阿司匹林 与 华法林 相互作用"），
放入新增的 instance_relations 数组，不要因为"relations 只在概念之间建立"而丢弃这类信息：
"instance_relations": [{"source": "实例名称", "target": "实例名称", "type": "causes|interacts_with|治疗|等英文或语义明确的关系类型", "confidence": 0.9}]
source/target 必须是 instances 数组里已出现的 name_cn，不要引用未提取的实例或概念实体本身。

属性的枚举值/分级/分档/阶段标签（如信用分层的"A层""B层""C层"、逾期阶段的"M0""M1""M2+"、
融资轮次的"A轮融资""D轮融资"）不是独立的概念，也不是需要单独建 instance 的命名对象——它们是所属
概念（如"信用分层""逾期阶段""融资"）某个属性的可选取值。不要为它们创建 entity，也不要创建
instance；只需在所属概念的 properties 里以枚举值形式体现（如信用分层.properties.levels = ["A层","B层","C层"]）。

业务规则、政策、判断条件、定价/审批/校验类规则（如"促销定价规则""审批超时规则""授信额度校验"）
不是实体，禁止作为 entity 或 instance 输出，必须放入 logic_rules 数组。"""


# ── Extraction results merge (方案A) ───────────────────────────────────────
def _merge_extraction_results(results: list[dict]) -> dict:
    """合并多个文件的 LLM 提取结果，按 name_cn 去重实体/规则/动作/实例。"""
    all_entities: list[dict] = []
    all_relations: list[dict] = []
    all_logic: list[dict] = []
    all_actions: list[dict] = []
    all_instances: list[dict] = []
    all_instance_relations: list[dict] = []

    # 收集全部
    for r in results:
        if not isinstance(r, dict):
            continue
        for e in (r.get("entities") or []):
            if isinstance(e, dict):
                all_entities.append(e)
        for rel in (r.get("relations") or []):
            if isinstance(rel, dict):
                # 跨文件时 source/target 可能是其他文件提取的实体，
                # 如果已存在则关系保留；暂存，后续去重
                all_relations.append(rel)
        for lg in (r.get("logic_rules") or []):
            if isinstance(lg, dict):
                all_logic.append(lg)
        for a in (r.get("actions") or []):
            if isinstance(a, dict):
                all_actions.append(a)
        for inst in (r.get("instances") or []):
            if isinstance(inst, dict):
                all_instances.append(inst)
        for ir in (r.get("instance_relations") or []):
            if isinstance(ir, dict):
                all_instance_relations.append(ir)

    # 实体去重（按 name_cn，保留属性最丰富的）
    seen_entities: dict[str, dict] = {}
    for e in all_entities:
        name = e.get("name_cn") or e.get("name", "")
        if not name:
            continue
        if name not in seen_entities:
            seen_entities[name] = e
        else:
            existing = seen_entities[name]
            existing_score = (
                len(str(existing.get("properties", {}))) +
                len(existing.get("description", "") or "")
            )
            cur_score = (
                len(str(e.get("properties", {}))) +
                len(e.get("description", "") or "")
            )
            if cur_score > existing_score:
                # 合并非冲突字段
                for k, v in e.items():
                    if v and k not in ("name_cn",) and not existing.get(k):
                        existing[k] = v

    entities = list(seen_entities.values())

    # 关系去重（source-target-type 唯一）
    seen_rels: set = set()
    relations = []
    for rel in all_relations:
        key = (rel.get("source", ""), rel.get("target", ""), rel.get("type", ""))
        if key not in seen_rels and key[0] and key[1]:
            seen_rels.add(key)
            relations.append(rel)

    # 逻辑规则去重
    seen_logic: dict[str, dict] = {}
    for lg in all_logic:
        name = lg.get("name_cn") or lg.get("name", "")
        if not name:
            continue
        if name not in seen_logic:
            seen_logic[name] = lg

    # 动作去重
    seen_actions: dict[str, dict] = {}
    for a in all_actions:
        name = a.get("name_cn") or a.get("name", "")
        if not name:
            continue
        if name not in seen_actions:
            seen_actions[name] = a

    # 实例去重（按 entity_type + name_cn 唯一）
    seen_instances: set = set()
    instances = []
    for inst in all_instances:
        key = (inst.get("entity_type", ""), inst.get("name_cn", ""))
        if key[0] and key[1] and key not in seen_instances:
            seen_instances.add(key)
            instances.append(inst)

    # 实例关系去重（source-target-type 唯一）
    seen_inst_rels: set = set()
    instance_relations = []
    for ir in all_instance_relations:
        key = (ir.get("source", ""), ir.get("target", ""), ir.get("type", ""))
        if key not in seen_inst_rels and key[0] and key[1]:
            seen_inst_rels.add(key)
            instance_relations.append(ir)

    return {
        "entities": entities,
        "relations": relations,
        "logic_rules": list(seen_logic.values()),
        "actions": list(seen_actions.values()),
        "instances": instances,
        "instance_relations": instance_relations,
    }



# ── Confidence calibration (Fix 5) ─────────────────────────────────────────
def _calibrate_confidence(result: dict) -> dict:
    """Adjust LLM-generated confidence scores using objective completeness signals."""
    entities    = result.get("entities", [])
    relations   = result.get("relations", [])
    logic_rules = result.get("logic_rules", [])
    actions     = result.get("actions", [])

    entity_names = {e.get("name_cn") for e in entities if e.get("name_cn")}

    # Entities that appear in at least one relation get a small boost
    in_graph: set = set()
    for r in relations:
        in_graph.add(r.get("source")); in_graph.add(r.get("target"))

    for e in entities:
        base = float(e.get("confidence") or 0.85)
        adj  = 0.0
        if not (e.get("properties") and len(e.get("properties", {})) > 0): adj -= 0.10
        if not (e.get("description") or "").strip():                        adj -= 0.05
        if e.get("name_cn") in in_graph:                                    adj += 0.05
        e["confidence"] = round(max(0.30, min(0.98, base + adj)), 3)

    for r in relations:
        base = float(r.get("confidence") or 0.85)
        if r.get("source") not in entity_names or r.get("target") not in entity_names:
            r["confidence"] = 0.30   # broken reference → low confidence
        else:
            r["confidence"] = round(max(0.40, min(0.98, base)), 3)

    logic_names = {r.get("name_cn") for r in logic_rules if r.get("name_cn")}
    valid_function_types = {"derived_property", "aggregation", "complex_edit", "external_query"}
    for rule in logic_rules:
        base = float(rule.get("confidence") or 0.85)
        adj  = 0.0
        if not rule.get("linked_entities"):                          adj -= 0.10
        if not (rule.get("definition") or "").strip():               adj -= 0.05
        if rule.get("function_type") not in valid_function_types:    adj -= 0.05
        rule["confidence"] = round(max(0.30, min(0.98, base + adj)), 3)

    for action in actions:
        base = float(action.get("confidence") or 0.85)
        adj  = 0.0
        if not action.get("parameters"):           adj -= 0.05
        if not action.get("rules"):                adj -= 0.20
        if not action.get("submission_criteria"):  adj -= 0.05
        if not action.get("linked_entities"):      adj -= 0.05
        action["confidence"] = round(max(0.30, min(0.98, base + adj)), 3)

    return result


def _resolve_name_abbr(e_data: dict, props: dict) -> str | None:
    abbr = e_data.get("name_abbr") or e_data.get("abbreviation")
    if not abbr and isinstance(props, dict):
        abbr = props.pop("abbreviation", None) or props.pop("abbr", None)
    if isinstance(abbr, str) and abbr.strip():
        return abbr.strip()
    return None


def _dedup_existing(db, ontology_id: str, model_cls, name_field: str):
    """Delete duplicate rows with the same (ontology_id, name_field), keeping the richest one."""
    rows = db.query(model_cls).filter(model_cls.ontology_id == ontology_id).all()
    seen: dict = {}
    for row in rows:
        key = getattr(row, name_field, None)
        if not key:
            continue
        if key not in seen:
            seen[key] = row
        else:
            # Keep the one with more data (prefer non-None properties/rules/definition)
            incumbent = seen[key]
            challenger_score = _richness(row)
            incumbent_score  = _richness(incumbent)
            if challenger_score > incumbent_score:
                db.delete(incumbent)
                seen[key] = row
            else:
                db.delete(row)


def _richness(obj) -> int:
    """Heuristic score for how data-rich an ORM object is — higher = keep."""
    score = 0
    for attr in ("properties", "rules", "definition", "description", "linked_entities"):
        val = getattr(obj, attr, None)
        if val:
            score += len(str(val))
    return score


def _fuzzy_resolve_entity(name: str, name_to_id: dict) -> str | None:
    """Resolve entity name to ID, falling back to substring-containment match.

    Handles cases where the LLM writes a slightly different name in relations
    than what was extracted in entities (e.g. '供应商' vs '供应商A').
    """
    if not name:
        return None
    if name in name_to_id:
        return name_to_id[name]
    # Substring containment: search name is contained in a known name, or vice versa
    candidates = [
        (kn, eid) for kn, eid in name_to_id.items()
        if kn and (name in kn or kn in name)
    ]
    if not candidates:
        return None
    # When multiple candidates, prefer the one sharing the most unique characters
    candidates.sort(key=lambda x: len(set(x[0]) & set(name)), reverse=True)
    return candidates[0][1]


# ── Tiered model selection (graph-engineering playbook, model selection) ────
# Extraction is high-volume and schema-constrained — speed/cost dominate, so
# it always uses whatever model the user picked. Resolution and relation
# inference weigh conflicting evidence — reasoning quality dominates — so
# when the user picked an explicitly fast/cheap variant, we look for a
# stronger sibling already configured on the same model config and use that
# instead. No new user-facing setting: this reads only what's already in
# ModelConfig.models.
_FAST_MODEL_HINTS = ("flash", "mini", "haiku", "lite", "turbo", "nano", "fast")


def _select_reasoning_model(model_name: str, available_models: list) -> str:
    """Pick the model to use for judgment-heavy stages (resolution, relation
    inference). Falls back to `model_name` unchanged unless the user's pick
    looks like a fast/cheap variant AND a non-fast sibling exists in the same
    model config — so behavior is identical to today whenever no better
    candidate is configured."""
    if not any(hint in model_name.lower() for hint in _FAST_MODEL_HINTS):
        return model_name
    for candidate in available_models or []:
        if isinstance(candidate, str) and candidate != model_name and not any(
            hint in candidate.lower() for hint in _FAST_MODEL_HINTS
        ):
            return candidate
    return model_name


# ── Long-document chunking (graph-engineering playbook, scaling guidance) ───
_CHUNK_MAX_CHARS = 12000
_CHUNK_OVERLAP_CHARS = 500
_SECTION_HEADER = re.compile(r"^#{1,3}\s+.*$", re.MULTILINE)


def _split_sections(md: str) -> list[str]:
    """Split markdown into section chunks at heading boundaries. Falls back to
    paragraph boundaries when the document has no headings at all."""
    starts = [m.start() for m in _SECTION_HEADER.finditer(md)]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    if len(starts) > 1:
        return [md[s:e] for s, e in zip(starts, starts[1:] + [len(md)]) if md[s:e].strip()]
    return [p for p in md.split("\n\n") if p.strip()]


def _chunk_document(md: str, max_chars: int = _CHUNK_MAX_CHARS, overlap_chars: int = _CHUNK_OVERLAP_CHARS) -> list[str]:
    """Chunk a long document at section boundaries so a single extraction call
    never has to hold the whole thing — entities/relations still land within
    the same chunk as their context. Each chunk after the first repeats the
    tail of the previous chunk as overlap, so a concept mentioned at a section
    boundary isn't split from the relations described just after it. Short
    documents (the common case) pass through unchanged as a single chunk."""
    if len(md) <= max_chars:
        return [md]

    sections = _split_sections(md)
    chunks: list[str] = []
    current = ""
    for section in sections:
        if current and len(current) + len(section) > max_chars:
            chunks.append(current)
            overlap = current[-overlap_chars:] if overlap_chars > 0 else ""
            current = overlap + section
        else:
            current += section
    if current.strip():
        chunks.append(current)
    return chunks or [md]


@celery_app.task(bind=True)
def run_extraction(self, task_id: str):
    import app.models  # noqa: F401 — register all tables for FK resolution
    from app.database import SessionLocal
    from app.models.extraction_task import ExtractionTask
    from app.models.file import UploadedFile
    from app.models.model_config import ModelConfig
    from app.models.prompt import Prompt
    from app.models.entity import Entity
    from app.models.logic import LogicRule
    from app.models.action import Action
    from app.models.relation import Relation
    from app.models.ontology import OntologyProject
    from app.services.llm_service import extract_ontology, infer_relations
    from app.services.encryption_service import decrypt
    import uuid

    db = SessionLocal()
    try:
        task = db.query(ExtractionTask).filter(ExtractionTask.id == task_id).first()
        if not task:
            return

        task.status = "running"
        task.progress = {"stage": "loading files", "pct": 10}
        db.commit()

        from app.services.document_service import combine_converted_files

        files = db.query(UploadedFile).filter(UploadedFile.ontology_id == task.ontology_id).all()
        if not files:
            task.status = "failed"; task.error = "No files uploaded"; db.commit(); return

        # 逐文件提取 + 合并 (方案A)：分文件提取避免多文件混合后 LLM 仅提取泛概念
        valid_mds = [f for f in files if (f.converted_md or "").strip()]
        if not valid_mds:
            task.status = "failed"; task.error = "No text content found in files"; db.commit(); return

        # 加载模型和提示词配置（必须在 LLM 调用前完成）
        model_cfg = db.query(ModelConfig).filter(ModelConfig.id == task.model_id).first()
        prompt    = db.query(Prompt).filter(Prompt.id == task.prompt_id).first()
        if not model_cfg or not prompt:
            task.status = "failed"; task.error = "Model or prompt not found"; db.commit(); return

        model_name = task.parameters.get("model_name", "")
        # resolution/relation-inference weigh conflicting evidence — use a
        # stronger sibling model already configured on this model config when
        # the user picked a fast/cheap variant for extraction (see
        # _select_reasoning_model); falls back to model_name unchanged
        # otherwise, so this is a no-op unless a better candidate exists
        reasoning_model_name = _select_reasoning_model(model_name, model_cfg.models)
        config_dict = {
            "provider": model_cfg.provider,
            "api_key":  decrypt(model_cfg.api_key_encrypted or ""),
            "api_base": model_cfg.api_base,
        }
        prompt_content = prompt.content + CONCEPT_INSTANCE_DIRECTIVE
        constraints = task.parameters.get("constraints", [])
        if constraints:
            prompt_content += "\n\n" + "\n".join(constraints)

        import re as _re
        def _clean(text: str) -> str:
            return _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

        combined_text = _clean("\n\n---\n\n".join(f.converted_md or "" for f in valid_mds if f.converted_md))

        # 并行提取所有文件 (I/O 密集型 LLM API 调用，ThreadPool 即可)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        all_results = []
        max_workers = 1  # serial extraction to avoid OOM with large LLM payloads
        completed = 0

        # a long document is chunked at section boundaries first (see
        # _chunk_document) — most documents are short and pass through as a
        # single chunk, so this is a no-op for today's typical corpus
        units = [(f, chunk) for f in valid_mds for chunk in _chunk_document(_clean(f.converted_md or ""))]
        total_units = len(units)

        task.progress = {"stage": f"extracting files 0/{total_units} (parallel ×{max_workers})", "pct": 20}
        db.commit()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for f, chunk in units:
                futures[executor.submit(extract_ontology, chunk, prompt_content, config_dict, model_name)] = f

            for future in as_completed(futures):
                f = futures[future]
                try:
                    single = future.result()
                    if isinstance(single, dict):
                        all_results.append(single)
                except Exception:
                    logger.exception("extract_ontology failed for file %s", getattr(f, "id", f))
                completed += 1
                task.progress = {"stage": f"extracting files {completed}/{total_units} (parallel ×{max_workers})", "pct": 20 + 35 * completed // total_units}
                db.commit()

        if all_results:
            result = _merge_extraction_results(all_results)
            # cross-file/single-file entity resolution (graph-engineering
            # playbook): the merge above only catches exact-name duplicates,
            # so a concept extracted under different surface forms — whether
            # across files or within one document — would otherwise fracture
            # into separate nodes
            new_entity_count = len(result.get("entities") or [])
            # incremental-update guidance: resolve this run's new entities
            # against the ontology's already-published entities too, so a
            # re-run doesn't create a duplicate for a surface-form variant of
            # something that already exists
            existing_entities_for_resolution = [
                {"name_cn": e.name_cn, "description": e.description, "type": e.type}
                for e in db.query(Entity).filter(Entity.ontology_id == task.ontology_id).all()
            ]
            if new_entity_count > 1 or (new_entity_count == 1 and existing_entities_for_resolution):
                task.progress = {"stage": "resolving entities", "pct": 58}
                db.commit()
                from app.services.llm_service import resolve_entities, apply_entity_resolution
                alias_map = resolve_entities(
                    result["entities"], config_dict, reasoning_model_name,
                    existing_entities=existing_entities_for_resolution,
                )
                result = apply_entity_resolution(result, alias_map)
        else:
            # 全部失败时回退到合并文本单次提取
            task.progress = {"stage": "calling LLM (combined fallback)", "pct": 55}
            db.commit()
            result = extract_ontology(combined_text, prompt_content, config_dict, model_name)

        # ── Fix 5: calibrate confidence before validation ────────────────────
        result = _calibrate_confidence(result)

        # ── P0 validation ────────────────────────────────────────────────────
        task.progress = {"stage": "validating output", "pct": 65}
        db.commit()

        from app.engine.post_harness.validator import PostHarnessValidator
        validator = PostHarnessValidator()
        v_report  = validator.validate(result)
        task.validation_report = v_report.to_dict()
        db.commit()

        if v_report.has_fatal():
            task.status = "failed"; task.error = v_report.to_summary(); db.commit(); return

        # ── Fix 1: second-pass relation inference ─────────────────────────────
        entities_extracted  = result.get("entities", [])
        relations_extracted = result.get("relations", [])
        entity_count    = len(entities_extracted)
        relation_count  = len(relations_extracted)

        # Count how many entities appear in at least one relation (exact or fuzzy)
        entity_names_set = {e.get("name_cn") for e in entities_extracted if e.get("name_cn")}
        in_relation: set = set()
        for r in relations_extracted:
            in_relation.add(r.get("source") or r.get("source_entity", ""))
            in_relation.add(r.get("target") or r.get("target_entity", ""))
        isolated_count = sum(
            1 for n in entity_names_set
            if n and not any(n in rn or rn in n for rn in in_relation if rn)
        )

        # Trigger when globally sparse OR >25% of entities are isolated
        sparse = relation_count < max(5, entity_count * 0.8)
        many_isolated = isolated_count > max(2, entity_count * 0.25)
        if entity_count >= 5 and (sparse or many_isolated):
            task.progress = {"stage": "inferring relations", "pct": 75}
            db.commit()
            extra_rels = infer_relations(
                entities_extracted, relations_extracted,
                combined_text, config_dict, reasoning_model_name
            )
            if extra_rels:
                # Accept relations where both endpoints fuzzy-match a known entity name
                for r in extra_rels:
                    src, tgt = r.get("source", ""), r.get("target", "")
                    src_ok = src in entity_names_set or any(
                        src in n or n in src for n in entity_names_set if n)
                    tgt_ok = tgt in entity_names_set or any(
                        tgt in n or n in tgt for n in entity_names_set if n)
                    if src_ok and tgt_ok:
                        result["relations"].append(r)
                result = _calibrate_confidence(result)

        task.progress = {"stage": "saving results", "pct": 85}
        db.commit()

        # ── Cleanup pre-existing duplicates (keep best, delete extras) ────────
        _dedup_existing(db, task.ontology_id, Entity, "name_cn")
        _dedup_existing(db, task.ontology_id, LogicRule, "name_cn")
        _dedup_existing(db, task.ontology_id, Action, "name_cn")
        db.flush()

        # ── Fix 2+4: upsert entities (by name_cn) ────────────────────────────
        existing_entities = db.query(Entity).filter(Entity.ontology_id == task.ontology_id).all()
        existing_ent_map  = {e.name_cn: e for e in existing_entities}

        entity_name_to_id: dict = {e.name_cn: e.id for e in existing_entities}
        for e in existing_entities:
            if e.name_en:
                entity_name_to_id[e.name_en] = e.id

        for e_data in result.get("entities", []):
            if not isinstance(e_data, dict):
                continue
            name_cn = e_data.get("name_cn") or e_data.get("name", "")
            if not name_cn:
                continue
            props = e_data.get("properties") or e_data.get("attributes") or e_data.get("attrs") or {}
            if not isinstance(props, dict):
                props = {}
            # 与 Pipeline Mapping 的概念实体约定对齐 — Entity 表只存概念，标记 is_concept
            props = {**props, "is_concept": True}
            name_abbr = _resolve_name_abbr(e_data, props)

            if name_cn in existing_ent_map:
                ent = existing_ent_map[name_cn]
                # Always allow LLM to enrich description, properties, name_en, name_abbr
                if e_data.get("description"): ent.description = e_data["description"]
                # merge, don't replace — a prior run's hub summary
                # (summary/key_facts/summary_degree) must survive a re-run
                # that doesn't touch this entity's properties otherwise
                if props:                     ent.properties  = {**(ent.properties or {}), **props}
                if e_data.get("name_en"):     ent.name_en     = e_data["name_en"]
                if name_abbr:                 ent.name_abbr   = name_abbr
                if e_data.get("type"):        ent.type        = e_data["type"]
                ent.confidence = e_data.get("confidence", ent.confidence)
                # Protect authoritative SNOMED fields — never overwrite once set
                if e_data.get("snomed_id") and not ent.snomed_id:
                    ent.snomed_id = e_data["snomed_id"]
                if e_data.get("canonical_id") and not ent.canonical_id:
                    ent.canonical_id = e_data["canonical_id"]
                eid = ent.id
            else:
                eid = str(uuid.uuid4())
                ent = Entity(
                    id=eid, ontology_id=task.ontology_id,
                    name_cn=name_cn, name_en=e_data.get("name_en"), name_abbr=name_abbr,
                    snomed_id=e_data.get("snomed_id"), canonical_id=e_data.get("canonical_id"),
                    type=e_data.get("type"), description=e_data.get("description"),
                    properties=props, confidence=e_data.get("confidence", 0.85),
                )
                db.add(ent)
                existing_ent_map[name_cn] = ent

            entity_name_to_id[name_cn] = eid
            if e_data.get("name_en"):
                entity_name_to_id[e_data["name_en"]] = eid

        # 实体写完先 commit 一次，缩短大事务窗口，避免 saving 阶段 SQLite 写冲突
        db.commit()

        # ── 写入实例数据（EntityInstance 表）— 挂在概念实体下，对齐 Pipeline Mapping ──
        from app.models.entity_instance import EntityInstance
        import hashlib as _hl
        # instance_relations (below) needs to resolve a source/target instance
        # name back to its stable id and owning concept entity id
        instance_name_to_id: dict = {}
        instance_id_to_entity_id: dict = {}
        for inst_data in result.get("instances", []):
            if not isinstance(inst_data, dict):
                continue
            inst_name = inst_data.get("name_cn") or inst_data.get("name", "")
            entity_type = inst_data.get("entity_type", "")
            if not inst_name or not entity_type:
                continue

            concept_id = _fuzzy_resolve_entity(entity_type, entity_name_to_id)
            if not concept_id:
                # LLM 引用了未在 entities 中声明的概念 — 自动创建，避免实例数据丢失
                concept_id = str(uuid.uuid4())
                db.add(Entity(
                    id=concept_id, ontology_id=task.ontology_id,
                    name_cn=entity_type, type=entity_type,
                    description=f"{entity_type} — 从实例数据自动创建的概念实体",
                    properties={"is_concept": True, "source": "auto"}, confidence=0.7,
                ))
                entity_name_to_id[entity_type] = concept_id

            inst_props = inst_data.get("properties") or {}
            if not isinstance(inst_props, dict):
                inst_props = {}
            inst_props = {
                **inst_props,
                "name_cn": inst_name,
                "name_en": inst_data.get("name_en", ""),
                "object_type": entity_type,
            }
            stable_id = str(uuid.UUID(_hl.md5(f"{task.ontology_id}:{concept_id}:{inst_name}".encode()).hexdigest()))
            db.merge(EntityInstance(
                id=stable_id, entity_id=concept_id, ontology_id=task.ontology_id,
                row_identity=inst_name, row_data=inst_props,
            ))
            instance_name_to_id[inst_name] = stable_id
            instance_id_to_entity_id[stable_id] = concept_id
        db.commit()

        # ── Fix 2+4: upsert relations (by source_id, target_id, type) ────────
        existing_rels    = db.query(Relation).filter(Relation.ontology_id == task.ontology_id).all()
        existing_rel_set = {(r.source_entity, r.target_entity, r.type) for r in existing_rels}
        relation_id_by_key: dict = {(r.source_entity, r.target_entity, r.type): r.id for r in existing_rels}

        for rel in result.get("relations", []):
            src_name = rel.get("source") or rel.get("source_entity", "")
            tgt_name = rel.get("target") or rel.get("target_entity", "")
            src_id   = _fuzzy_resolve_entity(src_name, entity_name_to_id)
            tgt_id   = _fuzzy_resolve_entity(tgt_name, entity_name_to_id)
            rel_type = (rel.get("type") or "RELATED").strip()
            # Reject fuzzy Chinese relation types — must be semantic English
            if rel_type in ("关联", "未知", "相关", "其他", "") or not rel_type.isascii():
                continue
            if src_id and tgt_id and (src_id, tgt_id, rel_type) not in existing_rel_set:
                new_rel_id = str(uuid.uuid4())
                db.add(Relation(
                    id=new_rel_id, ontology_id=task.ontology_id,
                    source_entity=src_id, target_entity=tgt_id,
                    type=rel_type, confidence=rel.get("confidence", 0.85),
                ))
                existing_rel_set.add((src_id, tgt_id, rel_type))
                relation_id_by_key[(src_id, tgt_id, rel_type)] = new_rel_id

        # ── instance-level relations (graph-engineering playbook): specific
        # named things (e.g. 2型糖尿病, 阿司匹林) keep their own relation graph
        # via EntityInstanceRelation, anchored to the concept-level Relation
        # it instantiates (auto-created here if the LLM didn't also emit it) ──
        from app.models.entity_instance_relation import EntityInstanceRelation
        existing_inst_rels = db.query(EntityInstanceRelation).filter(
            EntityInstanceRelation.ontology_id == task.ontology_id).all()
        existing_inst_rel_set = {
            (r.source_instance_id, r.target_instance_id, r.relation_definition_id)
            for r in existing_inst_rels
        }

        for ir in result.get("instance_relations", []):
            if not isinstance(ir, dict):
                continue
            src_iid = _fuzzy_resolve_entity(ir.get("source", ""), instance_name_to_id)
            tgt_iid = _fuzzy_resolve_entity(ir.get("target", ""), instance_name_to_id)
            if not src_iid or not tgt_iid:
                continue
            ir_type = (ir.get("type") or "RELATED").strip()
            if ir_type in ("关联", "未知", "相关", "其他", "") or not ir_type.isascii():
                continue
            src_concept_id = instance_id_to_entity_id.get(src_iid)
            tgt_concept_id = instance_id_to_entity_id.get(tgt_iid)
            if not src_concept_id or not tgt_concept_id:
                continue
            concept_key = (src_concept_id, tgt_concept_id, ir_type)
            rel_def_id = relation_id_by_key.get(concept_key)
            if not rel_def_id:
                rel_def_id = str(uuid.uuid4())
                db.add(Relation(
                    id=rel_def_id, ontology_id=task.ontology_id,
                    source_entity=src_concept_id, target_entity=tgt_concept_id,
                    type=ir_type, confidence=ir.get("confidence", 0.8),
                ))
                existing_rel_set.add(concept_key)
                relation_id_by_key[concept_key] = rel_def_id
                # the FK from entity_instance_relations to relations must see
                # this row on insert — flush now rather than trust autoflush
                # ordering across the two tables
                db.flush()
            edge_key = (src_iid, tgt_iid, rel_def_id)
            if edge_key not in existing_inst_rel_set:
                db.add(EntityInstanceRelation(
                    id=str(uuid.uuid4()), ontology_id=task.ontology_id,
                    source_instance_id=src_iid, target_instance_id=tgt_iid,
                    relation_definition_id=rel_def_id,
                    properties={"confidence": ir.get("confidence", 0.8)},
                ))
                existing_inst_rel_set.add(edge_key)

        # ── Keyword matching helpers (unchanged) ─────────────────────────────
        all_entity_names = [
            e.get("name_cn") or e.get("name", "")
            for e in result.get("entities", [])
            if e.get("name_cn") or e.get("name")
        ]
        type_to_entities: dict = {}
        for e in result.get("entities", []):
            etype = (e.get("type") or "").lower()
            ename = e.get("name_cn") or e.get("name", "")
            if ename:
                type_to_entities.setdefault(etype, []).append(ename)

        TYPE_KEYWORDS: dict = {
            "supplier": ["供应商","供货商","厂商","卖方"],
            "material": ["物料","原材料","辅料","零部件","库存"],
            "warehouse": ["仓库","库存","存储","盘点","入库","出库"],
            "product":  ["产品","成品","半成品","货物","质量","合格"],
            "document": ["订单","采购单","合同","审批","单据"],
            "process":  ["流程","工艺","步骤","采购","质检","物流"],
        }
        STOP_CHARS = set("的和在是了或且，。、（）[]【】")

        def _match_entities(text: str, entity_names: list) -> list:
            if not text: return []
            exact = [n for n in entity_names if n and n in text]
            if exact: return exact
            matched: list = []
            for etype, keywords in TYPE_KEYWORDS.items():
                if any(kw in text for kw in keywords):
                    matched.extend(type_to_entities.get(etype, []))
            return list(dict.fromkeys(matched))[:6]

        def _match_logic_rules(text: str, logic_name_to_id: dict) -> list:
            if not text: return []
            text_chars = set(text) - STOP_CHARS
            return [lid for lname, lid in logic_name_to_id.items()
                    if len(text_chars & (set(lname) - STOP_CHARS)) >= 2]

        # ── Fix 2+4: upsert logic rules (by name_cn) ─────────────────────────
        existing_rules    = db.query(LogicRule).filter(LogicRule.ontology_id == task.ontology_id).all()
        existing_rule_map = {r.name_cn: r for r in existing_rules}
        logic_name_to_id: dict = {r.name_cn: r.id for r in existing_rules}

        for r_data in result.get("logic_rules", []):
            if not isinstance(r_data, dict):
                continue
            name_cn = r_data.get("name_cn") or r_data.get("name", "")
            if not name_cn:
                continue

            llm_linked = r_data.get("linked_entities", [])
            if not llm_linked:
                combined = " ".join(filter(None, [name_cn, r_data.get("definition",""), r_data.get("description","")]))
                llm_linked = _match_entities(combined, all_entity_names)
            # A rule-category label (e.g. "反欺诈规则") is not a concept entity —
            # it belongs in logic_rules, not entities — so a linked_entities
            # reference to one is a dangling reference, same as a relation
            # pointing at an entity the model never extracted; drop it rather
            # than let it surface as a broken-reference validation warning.
            llm_linked = [n for n in llm_linked if n in entity_name_to_id]

            if name_cn in existing_rule_map:
                rule = existing_rule_map[name_cn]
                if r_data.get("function_type"): rule.function_type = r_data["function_type"]
                if r_data.get("definition"):    rule.definition    = r_data["definition"]
                if r_data.get("description"):   rule.description   = r_data["description"]
                if llm_linked:                  rule.linked_entities = llm_linked
                rule.confidence = r_data.get("confidence", rule.confidence)
                rid = rule.id
            else:
                rid  = str(uuid.uuid4())
                rule = LogicRule(
                    id=rid, ontology_id=task.ontology_id,
                    name_cn=name_cn, name_en=r_data.get("name_en"),
                    description=r_data.get("description"), function_type=r_data.get("function_type"),
                    definition=r_data.get("definition"),
                    confidence=r_data.get("confidence", 0.85),
                )
                rule.linked_entities = llm_linked
                db.add(rule)
                existing_rule_map[name_cn] = rule

            logic_name_to_id[name_cn] = rid

        # ── Fix 2+4: upsert actions (by name_cn) ─────────────────────────────
        existing_actions    = db.query(Action).filter(Action.ontology_id == task.ontology_id).all()
        existing_action_map = {a.name_cn: a for a in existing_actions}

        for a_data in result.get("actions", []):
            if not isinstance(a_data, dict):
                continue
            name_cn = a_data.get("name_cn") or a_data.get("name", "")
            if not name_cn:
                continue

            linked_ents = a_data.get("linked_entities", [])
            if not linked_ents:
                combined = " ".join(filter(None, [name_cn, a_data.get("description","")]))
                linked_ents = _match_entities(combined, all_entity_names)
            # Same dangling-reference cleanup as logic_rules above — a
            # rule-category label is not a concept entity.
            linked_ents = [n for n in linked_ents if n in entity_name_to_id]

            linked_logic_names = a_data.get("linked_logic_names", [])
            linked_ids = [logic_name_to_id[n] for n in linked_logic_names if n in logic_name_to_id]
            linked_ids += [i for i in a_data.get("linked_logic_ids", []) if i not in linked_ids]
            if not linked_ids:
                action_text = " ".join(filter(None, [name_cn, a_data.get("description","")]))
                linked_ids = _match_logic_rules(action_text, logic_name_to_id)

            if name_cn in existing_action_map:
                act = existing_action_map[name_cn]
                if a_data.get("description"):          act.description          = a_data["description"]
                if a_data.get("parameters"):            act.parameters            = a_data["parameters"]
                if a_data.get("rules"):                 act.rules                 = a_data["rules"]
                if a_data.get("submission_criteria"):   act.submission_criteria   = a_data["submission_criteria"]
                if a_data.get("side_effects"):          act.side_effects          = a_data["side_effects"]
                if a_data.get("name_en"):               act.name_en               = a_data["name_en"]
                if linked_ents:  act.linked_entities  = linked_ents
                if linked_ids:   act.linked_logic_ids = linked_ids
                act.confidence = a_data.get("confidence", act.confidence)
            else:
                act = Action(
                    id=str(uuid.uuid4()), ontology_id=task.ontology_id,
                    name_cn=name_cn, name_en=a_data.get("name_en"),
                    description=a_data.get("description"),
                    parameters=a_data.get("parameters") or [],
                    rules=a_data.get("rules") or [],
                    submission_criteria=a_data.get("submission_criteria") or [],
                    side_effects=a_data.get("side_effects") or [],
                    linked_entities=linked_ents, linked_logic_ids=linked_ids,
                    confidence=a_data.get("confidence", 0.85),
                )
                db.add(act)
                existing_action_map[name_cn] = act

        project = db.query(OntologyProject).filter(OntologyProject.id == task.ontology_id).first()
        if project:
            project.status = "created"

        # graph-engineering playbook diagnostics: computed over the ontology's
        # full saved graph (not just this run's output), so a stale connected-
        # components count from a prior run never masks today's actual state.
        # session autoflush is off (app/database.py), so this run's own
        # entity/relation adds above must be flushed before querying them back.
        db.flush()
        from app.services.graph_diagnostics import compute_graph_diagnostics
        final_entities = db.query(Entity).filter(Entity.ontology_id == task.ontology_id).all()
        final_relations = db.query(Relation).filter(Relation.ontology_id == task.ontology_id).all()
        graph_diagnostics = compute_graph_diagnostics(
            [{"id": e.id, "name_cn": e.name_cn} for e in final_entities],
            [(r.source_entity, r.target_entity) for r in final_relations],
        )
        task.validation_report = {**(task.validation_report or {}), "graph_diagnostics": graph_diagnostics}

        # hub-node summarization (playbook Section V.A): only entities with
        # enough graph connectivity to benefit from cross-relation synthesis;
        # re-summarizes only when degree changed since the last summary, so a
        # re-run doesn't re-call the model for an unchanged hub every time
        _HUB_DEGREE_THRESHOLD = 3
        degree_by_id: dict = {e.id: 0 for e in final_entities}
        relation_lines_by_id: dict = {}
        name_by_id = {e.id: e.name_cn for e in final_entities}
        for r in final_relations:
            if r.source_entity in degree_by_id:
                degree_by_id[r.source_entity] += 1
            if r.target_entity in degree_by_id:
                degree_by_id[r.target_entity] += 1
            line = f"({name_by_id.get(r.source_entity, r.source_entity)}) --[{r.type}]--> ({name_by_id.get(r.target_entity, r.target_entity)})"
            relation_lines_by_id.setdefault(r.source_entity, []).append(line)
            relation_lines_by_id.setdefault(r.target_entity, []).append(line)

        from app.services.llm_service import summarize_entity
        for e in final_entities:
            degree = degree_by_id.get(e.id, 0)
            if degree < _HUB_DEGREE_THRESHOLD:
                continue
            if (e.properties or {}).get("summary_degree") == degree:
                continue  # graph neighborhood unchanged since last summary
            relations_text = "\n".join(sorted(set(relation_lines_by_id.get(e.id, []))))
            profile = summarize_entity(e.name_cn, e.description or "", relations_text, config_dict, reasoning_model_name)
            if profile:
                e.properties = {**(e.properties or {}), **profile, "summary_degree": degree}

        task.status   = "completed"
        task.progress = {"stage": "done", "pct": 100}
        db.commit()

        # 同步到 Neo4j（非致命，失败不影响任务成功状态）
        _sync_neo4j(db, task.ontology_id)

    except Exception as e:
        logger.exception("extraction task %s failed", task_id)
        try:
            db.rollback()
            task = db.query(ExtractionTask).filter(ExtractionTask.id == task_id).first()
            if task:
                task.status = "failed"
                task.error  = str(e)
                db.commit()
        except Exception:
            # session 可能已损坏，用新 session 兜底标记失败，避免任务永远卡在 running
            logger.warning("primary session unusable, retrying with fresh session for task %s", task_id, exc_info=True)
            try:
                fresh_db = SessionLocal()
                task = fresh_db.query(ExtractionTask).filter(ExtractionTask.id == task_id).first()
                if task:
                    task.status = "failed"
                    task.error  = str(e)
                    fresh_db.commit()
                fresh_db.close()
            except Exception:
                logger.error("无法将任务 %s 标记为 failed，原始错误: %s", task_id, e, exc_info=True)
    finally:
        db.close()


def _sync_neo4j(db, ontology_id: str) -> None:
    """把 SQLite 里的实体/关系批量同步到 Neo4j。Neo4j 不可用或出错时静默跳过。"""
    try:
        from app.services.v2.graph.neo4j_service import Neo4jService
        from app.models.entity import Entity
        from app.models.relation import Relation
        from app.models.entity_instance import EntityInstance
        from app.models.entity_instance_relation import EntityInstanceRelation

        svc = Neo4jService()
        if not svc.available:
            return

        entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
        if not entities:
            svc.close()
            return

        # 按 type 分组批量 upsert（label = entity.type）
        from collections import defaultdict
        by_type: dict = defaultdict(list)
        entity_type_map: dict[str, str] = {}  # id → type
        for e in entities:
            label = e.type or "OntologyEntity"
            entity_type_map[e.id] = label
            props = {
                **(e.properties or {}),
                "id": e.id,
                "ontology_id": ontology_id,
                "name_cn": e.name_cn or "",
                "name_en": e.name_en or "",
                "name": e.name_cn or e.name_en or e.id,
                "display_name": e.name_cn or e.name_en or e.id,
                "type": label,
                "description": e.description or "",
                "confidence": e.confidence or 0.85,
            }
            by_type[label].append(props)

        for label, batch in by_type.items():
            svc.batch_upsert_entities(label, batch, key_field="id")

        # 写关系（逐条 MERGE，避免类型不匹配导致批量失败）
        relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
        for r in relations:
            src_type = entity_type_map.get(r.source_entity, "OntologyEntity")
            tgt_type = entity_type_map.get(r.target_entity, "OntologyEntity")
            rel_type = (r.type or "RELATED").upper().replace(" ", "_").replace("-", "_")
            svc.upsert_relation(
                src_label=src_type, src_key=r.source_entity,
                tgt_label=tgt_type, tgt_key=r.target_entity,
                rel_type=rel_type,
                props={"id": r.id, "ontology_id": ontology_id, "confidence": r.confidence or 0.85},
            )

        # 具体命名实例（如 2型糖尿病、阿司匹林）作为独立节点同步，标签在概念类型
        # 后加 Instance 后缀，与概念节点区分开，避免图查询把概念和实例混为一谈
        instances = db.query(EntityInstance).filter(EntityInstance.ontology_id == ontology_id).all()
        instance_label_map: dict[str, str] = {}
        for inst in instances:
            concept_type = entity_type_map.get(inst.entity_id, "OntologyEntity")
            label = f"{concept_type}Instance"
            instance_label_map[inst.id] = label
            props = {
                **(inst.row_data or {}),
                "id": inst.id,
                "ontology_id": ontology_id,
                "entity_id": inst.entity_id,
                "name_cn": inst.row_identity,
            }
            svc.upsert_entity(label, props, key_field="id")

        # 实例级关系（语义概念层面的 relation 已在上面写入；具体实例之间的边只
        # 存在于 Neo4j，不进 Postgres relations 表）
        inst_relations = db.query(EntityInstanceRelation).filter(
            EntityInstanceRelation.ontology_id == ontology_id).all()
        relation_type_by_id = {r.id: r.type for r in relations}
        for ir in inst_relations:
            src_label = instance_label_map.get(ir.source_instance_id, "OntologyEntityInstance")
            tgt_label = instance_label_map.get(ir.target_instance_id, "OntologyEntityInstance")
            rel_type = (relation_type_by_id.get(ir.relation_definition_id) or "RELATED").upper().replace(" ", "_").replace("-", "_")
            svc.upsert_relation(
                src_label=src_label, src_key=ir.source_instance_id,
                tgt_label=tgt_label, tgt_key=ir.target_instance_id,
                rel_type=rel_type,
                props={"id": ir.id, "ontology_id": ontology_id, **(ir.properties or {})},
            )

        svc.close()
    except Exception:
        logger.warning("Neo4j sync failed for ontology %s; extraction result unaffected", ontology_id, exc_info=True)
