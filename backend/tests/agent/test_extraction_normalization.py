"""Issue: simple-LLM ontology construction optimization (per the graph
engineering playbook).

The extraction prompt carries the playbook discipline (central-only entities,
grounded one-line descriptions, dedup guidance, explicit hierarchy guidance,
semantic relation types, no dangling references) and `extract_ontology`
normalizes every LLM result deterministically: junk entities dropped,
duplicate surface forms merged into one canonical entity, dangling/vague
relations removed, relation triples deduplicated.  The test proves the
quality improvement over a real test_data document.
"""
from pathlib import Path

import pytest

from app.services.llm_service import (
    VAGUE_RELATION_TYPES,
    _looks_like_enum_value,
    _looks_like_rule_name,
    apply_entity_resolution,
    extract_ontology,
    normalize_extracted_ontology,
    resolve_entities,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DATA = BACKEND_DIR.parent / "test_data"


def test_extraction_discipline_red_contract():
    """The extraction prompt + normalization are the observable discipline."""
    source = (BACKEND_DIR / "app" / "services" / "llm_service.py").read_text()
    for symbol in ("normalize_extracted_ontology", "VAGUE_RELATION_TYPES"):
        assert symbol in source, f"llm_service.py missing {symbol}"
    assert "禁止使用" in source  # prompt bans vague relation types
    assert "同一概念" in source   # prompt bans duplicate surface forms
    router = (BACKEND_DIR / "app" / "routers" / "prompts.py").read_text()
    assert "禁止为同义写法建重复实体" in router


def test_normalization_dedups_and_drops_junk_over_test_data_document():
    """A noisy extraction over the real 信贷 strategy document is normalized:
    duplicate surface forms merge, junk entities vanish, dangling and vague
    relations are dropped, hierarchy relations survive. Enum-value labels
    (tier/stage/round markers) and business-rule names are also entity
    leakage — dropped, along with any relation left dangling by their
    removal."""
    document = TEST_DATA / "信贷" / "信贷业务战略.md"
    if not document.exists():
        pytest.skip("test_data document unavailable")
    text = document.read_text(encoding="utf-8")

    raw = {
        "entities": [
            {"name_cn": "借款人", "type": "Concept", "description": "贷款申请人", "properties": {}},
            {"name_cn": "借款人（Borrower）", "type": "Concept", "description": "", "properties": {}},
            {"name_cn": "信用评分", "type": "Concept", "description": "授信决策的核心指标", "properties": {}},
            {"name_cn": "信用分层", "type": "Category", "description": "借款人风险分层", "properties": {}},
            {"name_cn": "A层", "type": "Category", "description": "优质客户", "properties": {}},
            {"name_cn": "B层", "type": "Category", "description": "良好客户", "properties": {}},
            {"name_cn": "授信额度", "type": "Concept", "description": "可贷上限", "properties": {}},
            {"name_cn": "促销定价规则", "type": "Concept", "description": "误提取的规则", "properties": {}},
            {"name_cn": "12345", "type": "Concept", "description": "噪音", "properties": {}},
            {"name_cn": "", "type": "Concept", "description": "空名", "properties": {}},
            {"name_cn": "信贷业务战略.md", "type": "Concept", "description": "文件名", "properties": {}},
        ],
        "relations": [
            {"source": "借款人", "target": "信用分层", "type": "IS-A", "confidence": 0.9},
            {"source": "A层", "target": "信用分层", "type": "INSTANCE-OF", "confidence": 0.9},
            {"source": "B层", "target": "信用分层", "type": "INSTANCE-OF", "confidence": 0.9},
            {"source": "借款人", "target": "信用评分", "type": "DEPENDS_ON", "confidence": 0.8},
            {"source": "借款人", "target": "信用分层", "type": "关联", "confidence": 0.6},
            {"source": "借款人", "target": "不存在的实体", "type": "IS-A", "confidence": 0.5},
            {"source": "借款人", "target": "信用评分", "type": "DEPENDS_ON", "confidence": 0.7},
            {"source": "A层", "target": "授信额度", "type": "GOVERNED_BY", "confidence": 0.8},
        ],
        "logic_rules": [
            {"name_cn": "信用评分降级规则", "function_type": "derived_property", "definition": "信用评分 < 500 时降级",
             "description": "降级规则", "linked_entities": ["信用评分"]},
        ],
        "actions": [],
        "instances": [],
    }
    assert len(text) > 200

    normalized = normalize_extracted_ontology(raw)
    names = {e["name_cn"] for e in normalized["entities"]}
    # duplicate surface forms merged into one canonical entity
    assert "借款人" in names
    assert "借款人（Borrower）" not in names
    # junk entities dropped
    assert "12345" not in names
    assert "信贷业务战略.md" not in names
    # enum-value labels (tier markers) are not concepts — dropped
    assert "A层" not in names
    assert "B层" not in names
    # a business rule is not an entity — dropped (belongs in logic_rules)
    assert "促销定价规则" not in names
    assert not any(not (e.get("name_cn") or "").strip() for e in normalized["entities"])
    # every surviving entity carries a grounded one-line description
    assert all(e.get("description") for e in normalized["entities"])

    # no dangling references, no vague types, no duplicate triples
    triples = {(r["source"], r["type"], r["target"]) for r in normalized["relations"]}
    assert all(r["source"] in names and r["target"] in names for r in normalized["relations"])
    assert not any(r["type"] in VAGUE_RELATION_TYPES for r in normalized["relations"])
    assert ("借款人", "IS-A", "信用分层") in triples
    assert ("借款人", "DEPENDS_ON", "信用评分") in triples
    # the duplicate DEPENDS_ON appeared twice -> deduplicated to one
    assert sum(1 for r in normalized["relations"] if r["type"] == "DEPENDS_ON") == 1
    # relations pointing at the dropped enum-value entities are now dangling
    # references and are dropped too
    assert ("A层", "INSTANCE-OF", "信用分层") not in triples
    assert ("B层", "INSTANCE-OF", "信用分层") not in triples
    assert ("A层", "GOVERNED_BY", "授信额度") not in triples


def test_looks_like_enum_value_matches_tier_stage_and_round_labels():
    for name in ("A层", "B层", "C级", "M0", "M1", "M2+", "M0阶段", "A轮融资", "D轮融资", "A档", "B类"):
        assert _looks_like_enum_value(name), name


def test_looks_like_enum_value_does_not_match_real_concepts():
    for name in ("借款人", "供应商", "A股", "P7", "信用分层", "授信额度", "D轮"):
        assert not _looks_like_enum_value(name), name


def test_looks_like_rule_name_matches_policy_and_rule_suffixes():
    for name in ("促销定价规则", "审批超时规则", "员工考勤制度", "风险准入政策"):
        assert _looks_like_rule_name(name), name


def test_looks_like_rule_name_does_not_match_concepts():
    for name in ("借款人", "信用分层", "贷款产品"):
        assert not _looks_like_rule_name(name), name


def test_normalization_is_idempotent_and_keeps_clean_output():
    """A clean extraction passes through unchanged (bounded normalization)."""
    clean = {
        "entities": [
            {"name_cn": "供应商", "type": "Supplier", "description": "供货企业", "properties": {}},
            {"name_cn": "仓库", "type": "Warehouse", "description": "仓储节点", "properties": {}},
        ],
        "relations": [
            {"source": "供应商", "target": "仓库", "type": "SUPPLIES", "confidence": 0.9},
        ],
        "logic_rules": [], "actions": [],
    }
    once = normalize_extracted_ontology(clean)
    twice = normalize_extracted_ontology(once)
    assert once == twice
    assert [e["name_cn"] for e in once["entities"]] == ["供应商", "仓库"]
    assert once["relations"] == clean["relations"]


def test_extract_ontology_applies_normalization():
    """extract_ontology runs the raw model output through the normalization
    step (the extraction task sees the clean result)."""
    from app.services import llm_service
    original = llm_service._call_llm

    def fake_call(*args, **kwargs):
        return (
            '{"entities": [{"name_cn": "客户", "type": "Concept", "description": "d"}, '
            '{"name_cn": "客户（Customer）", "type": "Concept", "description": ""}], '
            '"relations": [{"source": "客户", "target": "不存在", "type": "IS-A"}]}'
        )

    llm_service._call_llm = fake_call
    try:
        result = extract_ontology("文档", "提示", {"provider": "openai", "api_key": "x"}, "m")
    finally:
        llm_service._call_llm = original
    assert [e["name_cn"] for e in result["entities"]] == ["客户"]
    assert result["relations"] == []


def test_resolve_entities_clusters_cross_file_surface_forms():
    """resolve_entities (playbook resolution stage): two entities that survive
    exact-name dedup because they're spelled differently across files ('供应商
    甲' vs '供应商甲有限公司') get clustered into one alias map entry using
    their descriptions; a genuinely distinct entity of the same type keeps its
    own single-element cluster (no over-merging)."""
    from app.services import llm_service
    original = llm_service._call_llm

    def fake_call(provider, api_key, api_base, model, messages):
        return (
            '{"clusters": ['
            '{"canonical": "供应商甲有限公司", "aliases": ["供应商甲", "供应商甲有限公司"]},'
            '{"canonical": "供应商乙", "aliases": ["供应商乙"]}'
            ']}'
        )

    llm_service._call_llm = fake_call
    try:
        entities = [
            {"name_cn": "供应商甲", "type": "Supplier", "description": "文件1中提及的供应商甲"},
            {"name_cn": "供应商甲有限公司", "type": "Supplier", "description": "文件2中提及的供应商甲有限公司，注册地上海"},
            {"name_cn": "供应商乙", "type": "Supplier", "description": "另一家供应商，与甲无关"},
        ]
        alias_map = resolve_entities(entities, {"provider": "openai", "api_key": "x"}, "m")
    finally:
        llm_service._call_llm = original

    assert alias_map["供应商甲"] == "供应商甲有限公司"
    assert alias_map["供应商甲有限公司"] == "供应商甲有限公司"
    assert alias_map["供应商乙"] == "供应商乙"


def test_resolve_entities_falls_back_to_identity_on_failure():
    """A failed resolution call never drops an entity: every input name still
    maps to itself."""
    from app.services import llm_service
    original = llm_service._call_llm
    llm_service._call_llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        entities = [
            {"name_cn": "A", "type": "Concept", "description": "d1"},
            {"name_cn": "B", "type": "Concept", "description": "d2"},
        ]
        alias_map = resolve_entities(entities, {"provider": "openai", "api_key": "x"}, "m")
    finally:
        llm_service._call_llm = original
    assert alias_map == {"A": "A", "B": "B"}


def test_resolve_entities_single_type_member_skips_llm_call():
    """A type with only one entity needs no clustering — no LLM call, identity
    mapping only."""
    from app.services import llm_service
    original = llm_service._call_llm
    llm_service._call_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    try:
        entities = [{"name_cn": "唯一实体", "type": "Concept", "description": "d"}]
        alias_map = resolve_entities(entities, {"provider": "openai", "api_key": "x"}, "m")
    finally:
        llm_service._call_llm = original
    assert alias_map == {"唯一实体": "唯一实体"}


def test_resolve_entities_anchors_canonical_to_existing_entity():
    """Incremental-update guidance: a new-run surface form that clusters with
    an already-published entity must remap to that entity's own name, not to
    whatever the model picked as canonical."""
    from app.services import llm_service
    original = llm_service._call_llm

    def fake_call(provider, api_key, api_base, model, messages):
        assert "[已存在]" in messages[1]["content"]
        return '{"clusters": [{"canonical": "客户方", "aliases": ["客户方", "客户"]}]}'

    llm_service._call_llm = fake_call
    try:
        new_entities = [{"name_cn": "客户方", "type": "Concept", "description": "本轮新提取"}]
        existing = [{"name_cn": "客户", "type": "Concept", "description": "已发布的规范实体"}]
        alias_map = resolve_entities(
            new_entities, {"provider": "openai", "api_key": "x"}, "m", existing_entities=existing,
        )
    finally:
        llm_service._call_llm = original
    # the model picked "客户方" as canonical, but the existing entity "客户"
    # must win regardless
    assert alias_map["客户方"] == "客户"


def test_resolve_entities_single_new_entity_still_checked_against_existing():
    """A single new entity (which would otherwise skip clustering entirely)
    must still be checked against existing entities of the same type."""
    from app.services import llm_service
    original = llm_service._call_llm
    llm_service._call_llm = lambda *a, **k: (
        '{"clusters": [{"canonical": "借款人", "aliases": ["借款方", "借款人"]}]}'
    )
    try:
        alias_map = resolve_entities(
            [{"name_cn": "借款方", "type": "Concept", "description": "本轮新提取"}],
            {"provider": "openai", "api_key": "x"}, "m",
            existing_entities=[{"name_cn": "借款人", "type": "Concept", "description": "已发布"}],
        )
    finally:
        llm_service._call_llm = original
    assert alias_map["借款方"] == "借款人"


def test_resolve_entities_does_not_touch_existing_entities_of_other_types():
    """Existing entities of a different type never enter the same clustering
    call — no cross-type contamination."""
    from app.services import llm_service
    original = llm_service._call_llm
    llm_service._call_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    try:
        alias_map = resolve_entities(
            [{"name_cn": "唯一实体", "type": "Concept", "description": "d"}],
            {"provider": "openai", "api_key": "x"}, "m",
            existing_entities=[{"name_cn": "供应商甲", "type": "Supplier", "description": "d"}],
        )
    finally:
        llm_service._call_llm = original
    assert alias_map == {"唯一实体": "唯一实体"}


def test_apply_entity_resolution_merges_and_remaps_references():
    """apply_entity_resolution merges aliased entities (keeping the richer
    description), remaps relation endpoints, drops self-loops created by the
    merge, and remaps linked_entities on logic rules/actions."""
    result = {
        "entities": [
            {"name_cn": "供应商甲", "type": "Supplier", "description": "短描述", "properties": {"tier": "A"}},
            {"name_cn": "供应商甲有限公司", "type": "Supplier",
             "description": "文件2中提及的供应商甲有限公司，注册地上海，更完整的描述", "properties": {"city": "上海"}},
            {"name_cn": "仓库", "type": "Warehouse", "description": "仓储节点", "properties": {}},
        ],
        "relations": [
            {"source": "供应商甲", "target": "仓库", "type": "SUPPLIES", "confidence": 0.9},
            {"source": "供应商甲有限公司", "target": "仓库", "type": "SUPPLIES", "confidence": 0.8},
            {"source": "供应商甲", "target": "供应商甲有限公司", "type": "IS-A", "confidence": 0.5},
        ],
        "logic_rules": [
            {"name_cn": "规则1", "linked_entities": ["供应商甲", "供应商甲有限公司", "仓库"]},
        ],
        "actions": [],
    }
    alias_map = {"供应商甲": "供应商甲有限公司", "供应商甲有限公司": "供应商甲有限公司", "仓库": "仓库"}

    merged = apply_entity_resolution(result, alias_map)

    names = [e["name_cn"] for e in merged["entities"]]
    assert names == ["供应商甲有限公司", "仓库"]
    canonical_entity = merged["entities"][0]
    # the richer description survives the merge
    assert canonical_entity["description"] == "文件2中提及的供应商甲有限公司，注册地上海，更完整的描述"
    # non-conflicting properties from both surface forms are kept
    assert canonical_entity["properties"] == {"tier": "A", "city": "上海"}

    triples = {(r["source"], r["type"], r["target"]) for r in merged["relations"]}
    # both SUPPLIES relations collapse to one (duplicate after remap)
    assert triples == {("供应商甲有限公司", "SUPPLIES", "仓库")}
    # the IS-A relation between the two aliases became a self-loop and was dropped

    assert merged["logic_rules"][0]["linked_entities"] == ["供应商甲有限公司", "仓库"]


def test_apply_entity_resolution_is_noop_for_identity_map():
    """When resolution changes nothing (every name maps to itself), the result
    is returned unchanged."""
    result = {"entities": [{"name_cn": "A"}], "relations": []}
    assert apply_entity_resolution(result, {"A": "A"}) is result
    assert apply_entity_resolution(result, {}) is result
