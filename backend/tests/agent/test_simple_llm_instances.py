"""Issue 6: simple-LLM extraction materializes EntityInstance rows.

A simple-LLM ontology over a tabular (CSV) test_data file must end up with
instance data attached to its concept entities (row_identity + row_data),
consistent with Pipeline/Mapping output — not just Entity/Relation rows.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def _scoped_url(schema: str) -> str:
    return f"{TEST_DATABASE_URL}?options={quote(f'-csearch_path={schema},public', safe='-=,')}"


def _alembic(schema: str, *args, check=True):
    return subprocess.run(
        [sys.executable, "scripts/run_migrations.py", *args],
        cwd=BACKEND_DIR,
        env=dict(os.environ, DATABASE_URL=_scoped_url(schema)),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "simple_llm_inst_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    assert _alembic(schema, "upgrade", "head").returncode == 0
    yield schema
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


# a tabular 信贷 CSV rendered to markdown (exactly what convert_document does)
TABULAR_MD = (
    "# 贷款申请记录\n\n"
    "| 客户名称 | 贷款金额 | 状态 |\n"
    "|---|---|---|\n"
    "| 上海华瑞银行 | 500000 | 审批中 |\n"
    "| 南京银行 | 1200000 | 已放款 |\n"
    "| 众邦银行 | 300000 | 已结清 |\n"
)


def _seed(session, *, ontology_id="o-llm", editor_id="u-1"):
    session.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,security_domain_id,created_at,updated_at) "
        "VALUES (:u,'llm','l@t.com','h','editor',true,:d,now(),now())"
    ), {"u": editor_id, "d": DEFAULT_DOMAIN})
    session.execute(text(
        "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at,security_domain_id,working_revision) "
        "VALUES (:o,'信贷-LLM提取','credit','v1','created',:u,now(),now(),:d,1)"
    ), {"o": ontology_id, "u": editor_id, "d": DEFAULT_DOMAIN})
    session.execute(text(
        "INSERT INTO model_configs (id,name,config_type,api_base,api_key_encrypted,provider,models,options,created_by,created_at,updated_at) "
        "VALUES ('m-1','m','llm',NULL,'','openai','[]'::json,'{}'::json,:u,now(),now())"
    ), {"u": editor_id})
    session.execute(text(
        "INSERT INTO model_config_versions (id, model_config_id, version_no, provider, options, behavior_hash, model_contract, created_at) "
        "VALUES ('mv-1', 'm-1', 1, 'openai', '{}'::json, :hash, '[]'::json, now())"
    ), {"hash": "0" * 64})
    session.execute(text(
        "UPDATE model_configs SET active_version_id = 'mv-1', status = 'active' WHERE id = 'm-1'"
    ))
    session.execute(text(
        "INSERT INTO prompts (id, name, domain, content, version, created_by, created_at, updated_at) "
        "VALUES ('p-1', 'extract', 'credit', :content, 'v1', :u, now(), now())"
    ), {"content": "从文档提取本体 JSON", "u": editor_id})
    session.execute(text(
        "INSERT INTO uploaded_files (id, ontology_id, filename, file_path, file_size, mime_type, converted_md, created_at) "
        "VALUES ('f-1', :o, '贷款申请记录.csv', '/tmp/loan.csv', 100, 'text/csv', :md, now())"
    ), {"o": ontology_id, "md": TABULAR_MD})
    task_id = str(uuid.uuid4())
    session.execute(text(
        "INSERT INTO extraction_tasks (id, ontology_id, prompt_id, model_id, status, parameters, progress, error, created_at, updated_at) "
        "VALUES (:id, :o, 'p-1', 'm-1', 'queued', :params, '{}'::json, NULL, now(), now())"
    ), {"id": task_id, "o": ontology_id, "params": '{"model_name": "mock-extractor", "constraints": []}'})
    session.commit()
    return task_id


def _worker(schema):
    """Session factory patched in for app.database.SessionLocal (worker-loop
    pattern) plus one open session for seeding/asserting."""
    factory = sessionmaker(bind=create_engine(_scoped_url(schema)))
    return factory, factory()


def test_simple_llm_tabular_extraction_materializes_instances(schema, monkeypatch):
    """The simple-LLM extraction task, over a tabular test_data file, must
    create EntityInstance rows (row_identity) attached to the concept entities
    — consistent with Pipeline/Mapping output."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)

    # deterministic LLM output: concepts + named instances from the table
    fake_result = {
        "entities": [
            {"name_cn": "客户", "type": "客户", "description": "贷款客户", "properties": {}},
            {"name_cn": "贷款申请", "type": "贷款申请", "description": "贷款申请记录", "properties": {}},
        ],
        "relations": [
            {"source": "客户", "target": "贷款申请", "type": "APPLIES_FOR", "confidence": 0.9},
        ],
        "logic_rules": [],
        "actions": [],
        "instances": [
            {"entity_type": "客户", "name_cn": "上海华瑞银行", "properties": {"region": "上海"}},
            {"entity_type": "客户", "name_cn": "南京银行", "properties": {"region": "江苏"}},
            {"entity_type": "客户", "name_cn": "众邦银行", "properties": {"region": "湖北"}},
            {"entity_type": "贷款申请", "name_cn": "贷款申请-500000", "properties": {"amount": 500000, "status": "审批中"}},
        ],
    }
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: fake_result,
    )

    extraction_task.run_extraction(task_id)

    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]
        instances = session.execute(text(
            "SELECT ei.row_identity, ei.row_data, e.name_cn AS entity_name "
            "FROM entity_instances ei JOIN entities e ON e.id = ei.entity_id "
            "WHERE ei.ontology_id = 'o-llm' ORDER BY ei.row_identity"
        )).mappings().all()
        # the four named instances from the tabular data are materialized
        assert {i["row_identity"] for i in instances} == {
            "上海华瑞银行", "南京银行", "众邦银行", "贷款申请-500000",
        }
        assert all(i["entity_name"] in ("客户", "贷款申请") for i in instances)
        row_data = {i["row_identity"]: i["row_data"] for i in instances}
        assert row_data["上海华瑞银行"]["name_cn"] == "上海华瑞银行"
        assert row_data["贷款申请-500000"]["amount"] == 500000
        assert row_data["贷款申请-500000"]["object_type"] == "贷款申请"
    finally:
        session.close()


def test_instance_relations_create_concept_relation_and_instance_edge(schema, monkeypatch):
    """A causal/interaction relation between two SPECIFIC named instances
    (e.g. 2型糖尿病 causes 糖尿病肾病) must not be silently dropped just
    because 'relations only connect concepts' — it auto-creates the concept-
    level Relation it instantiates and lands as an EntityInstanceRelation
    edge between the two instance rows."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task

    fake_result = {
        "entities": [{"name_cn": "疾病", "type": "Disease", "description": "d", "properties": {}}],
        "relations": [], "logic_rules": [], "actions": [],
        "instances": [
            {"entity_type": "疾病", "name_cn": "2型糖尿病", "properties": {}},
            {"entity_type": "疾病", "name_cn": "糖尿病肾病", "properties": {}},
        ],
        "instance_relations": [
            {"source": "2型糖尿病", "target": "糖尿病肾病", "type": "causes", "confidence": 0.9},
        ],
    }
    monkeypatch.setattr("app.services.llm_service.extract_ontology", lambda *a, **k: fake_result)

    extraction_task.run_extraction(task_id)
    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]

        concept_rels = session.execute(text(
            "SELECT type FROM relations WHERE ontology_id = 'o-llm'"
        )).mappings().all()
        assert [r["type"] for r in concept_rels] == ["causes"]

        edge = session.execute(text(
            "SELECT ei_src.row_identity AS src, ei_tgt.row_identity AS tgt "
            "FROM entity_instance_relations eir "
            "JOIN entity_instances ei_src ON ei_src.id = eir.source_instance_id "
            "JOIN entity_instances ei_tgt ON ei_tgt.id = eir.target_instance_id "
            "WHERE eir.ontology_id = 'o-llm'"
        )).mappings().one()
        assert edge["src"] == "2型糖尿病"
        assert edge["tgt"] == "糖尿病肾病"
    finally:
        session.close()


def test_instance_relations_rerun_does_not_duplicate(schema, monkeypatch):
    """Re-running extraction with the same instance_relations output must not
    create a second EntityInstanceRelation edge or a second concept Relation."""
    factory, session = _worker(schema)
    task_id_1 = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task

    fake_result = {
        "entities": [{"name_cn": "疾病", "type": "Disease", "description": "d", "properties": {}}],
        "relations": [], "logic_rules": [], "actions": [],
        "instances": [
            {"entity_type": "疾病", "name_cn": "2型糖尿病", "properties": {}},
            {"entity_type": "疾病", "name_cn": "糖尿病肾病", "properties": {}},
        ],
        "instance_relations": [
            {"source": "2型糖尿病", "target": "糖尿病肾病", "type": "causes", "confidence": 0.9},
        ],
    }
    monkeypatch.setattr("app.services.llm_service.extract_ontology", lambda *a, **k: fake_result)
    extraction_task.run_extraction(task_id_1)

    task_id_2 = str(uuid.uuid4())
    session.execute(text(
        "INSERT INTO extraction_tasks (id, ontology_id, prompt_id, model_id, status, parameters, progress, error, created_at, updated_at) "
        "VALUES (:id, 'o-llm', 'p-1', 'm-1', 'queued', :params, '{}'::json, NULL, now(), now())"
    ), {"id": task_id_2, "params": '{"model_name": "mock-extractor", "constraints": []}'})
    session.commit()
    monkeypatch.setattr(
        "app.services.llm_service._call_llm",
        lambda *a, **k: '{"clusters": []}',
    )
    extraction_task.run_extraction(task_id_2)

    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id_2}).mappings().one()
        assert status["status"] == "completed", status["error"]
        rel_count = session.execute(text(
            "SELECT count(*) FROM relations WHERE ontology_id = 'o-llm'"
        )).scalar_one()
        edge_count = session.execute(text(
            "SELECT count(*) FROM entity_instance_relations WHERE ontology_id = 'o-llm'"
        )).scalar_one()
        assert rel_count == 1
        assert edge_count == 1
    finally:
        session.close()


def test_single_document_near_duplicate_entities_are_resolved(schema, monkeypatch):
    """Sub-project E3 bug 1: a single-file extraction with near-duplicate
    entity names (e.g. "产品研发部" vs "产品研发部门") must still go through
    entity resolution — previously gated behind len(all_results) > 1, so a
    single document's duplicates were never merged."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task

    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [
                {"name_cn": "产品研发部", "type": "Organization", "description": "负责产品研发", "properties": {}},
                {"name_cn": "产品研发部门", "type": "Organization", "description": "", "properties": {}},
            ],
            "relations": [],
            "logic_rules": [],
            "actions": [],
        },
    )
    monkeypatch.setattr(
        "app.services.llm_service.resolve_entities",
        lambda entities, config, model_name, existing_entities=None: {
            "产品研发部": "产品研发部",
            "产品研发部门": "产品研发部",
        },
    )

    extraction_task.run_extraction(task_id)

    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]
        names = session.execute(text(
            "SELECT name_cn FROM entities WHERE ontology_id = 'o-llm'"
        )).scalars().all()
        assert names == ["产品研发部"]
    finally:
        session.close()


def test_simple_llm_without_instances_still_succeeds(schema, monkeypatch):
    """An LLM result without an instances array must not break extraction —
    entities/relations still land, the task completes."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [{"name_cn": "客户", "type": "客户", "description": "贷款客户", "properties": {}}],
            "relations": [],
            "logic_rules": [],
            "actions": [],
        },
    )
    extraction_task.run_extraction(task_id)
    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]
        assert session.execute(text(
            "SELECT count(*) FROM entities WHERE ontology_id = 'o-llm'"
        )).scalar_one() == 1
        assert session.execute(text(
            "SELECT count(*) FROM entity_instances WHERE ontology_id = 'o-llm'"
        )).scalar_one() == 0
    finally:
        session.close()


def test_dangling_linked_entities_reference_is_dropped(schema, monkeypatch):
    """A logic_rule/action whose linked_entities names a rule-category label
    (e.g. "反欺诈规则") rather than a real concept entity must not carry that
    dangling reference into the saved row — the same discipline already
    applied to relations pointing at an entity the model never extracted."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [{"name_cn": "设备指纹", "type": "Concept", "description": "设备指纹特征", "properties": {}}],
            "relations": [],
            "logic_rules": [
                {"name_cn": "设备指纹黑名单拒绝", "function_type": "derived_property", "definition": "命中黑名单则拒绝",
                 "linked_entities": ["设备指纹", "反欺诈规则"]},
            ],
            "actions": [
                {"name_cn": "标记欺诈", "rules": [], "linked_entities": ["设备指纹", "反欺诈规则"]},
            ],
        },
    )
    extraction_task.run_extraction(task_id)
    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]
        def _as_list(value):
            return json.loads(value) if isinstance(value, str) else value

        rule_linked = _as_list(session.execute(text(
            "SELECT linked_entities FROM logic_rules WHERE ontology_id = 'o-llm' AND name_cn = '设备指纹黑名单拒绝'"
        )).scalar_one())
        assert rule_linked == ["设备指纹"]
        action_linked = _as_list(session.execute(text(
            "SELECT linked_entities FROM actions WHERE ontology_id = 'o-llm' AND name_cn = '标记欺诈'"
        )).scalar_one())
        assert action_linked == ["设备指纹"]
    finally:
        session.close()


def test_rerun_resolves_new_surface_form_against_already_published_entity(schema, monkeypatch):
    """Incremental-update guidance: re-running extraction on an ontology that
    already has a published entity must not create a duplicate when the new
    run's LLM output spells the same concept differently — it should resolve
    to the existing entity's own name, not the other way around."""
    factory, session = _worker(schema)
    task_id_1 = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task

    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [{"name_cn": "客户", "type": "Concept", "description": "贷款客户", "properties": {}}],
            "relations": [], "logic_rules": [], "actions": [],
        },
    )
    extraction_task.run_extraction(task_id_1)

    task_id_2 = str(uuid.uuid4())
    session.execute(text(
        "INSERT INTO extraction_tasks (id, ontology_id, prompt_id, model_id, status, parameters, progress, error, created_at, updated_at) "
        "VALUES (:id, 'o-llm', 'p-1', 'm-1', 'queued', :params, '{}'::json, NULL, now(), now())"
    ), {"id": task_id_2, "params": '{"model_name": "mock-extractor", "constraints": []}'})
    session.commit()

    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [{"name_cn": "客户方", "type": "Concept", "description": "本轮换了个写法", "properties": {}}],
            "relations": [], "logic_rules": [], "actions": [],
        },
    )
    monkeypatch.setattr(
        "app.services.llm_service._call_llm",
        lambda *a, **k: '{"clusters": [{"canonical": "客户方", "aliases": ["客户方", "客户"]}]}',
    )
    extraction_task.run_extraction(task_id_2)

    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id_2}).mappings().one()
        assert status["status"] == "completed", status["error"]
        names = session.execute(text(
            "SELECT name_cn FROM entities WHERE ontology_id = 'o-llm'"
        )).scalars().all()
        # the model's clustering call picked "客户方" as canonical, but the
        # already-published "客户" must win — no duplicate, no rename
        assert names == ["客户"]
    finally:
        session.close()


def test_extraction_task_stores_graph_diagnostics(schema, monkeypatch):
    """Graph diagnostics (connected components, hub entities) are computed
    over the ontology's full saved graph and stored on the task's
    validation_report alongside the existing P0 report."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [
                {"name_cn": "借款人", "type": "Concept", "description": "d", "properties": {}},
                {"name_cn": "贷款", "type": "Concept", "description": "d", "properties": {}},
                {"name_cn": "孤立实体", "type": "Concept", "description": "d", "properties": {}},
            ],
            "relations": [{"source": "借款人", "target": "贷款", "type": "APPLIES_FOR", "confidence": 0.9}],
            "logic_rules": [], "actions": [],
        },
    )
    extraction_task.run_extraction(task_id)
    try:
        report = session.execute(text(
            "SELECT validation_report FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).scalar_one()
        diagnostics = json.loads(report)["graph_diagnostics"] if isinstance(report, str) else report["graph_diagnostics"]
        assert diagnostics["entity_count"] == 3
        assert diagnostics["edge_count"] == 1
        assert diagnostics["connected_components"] == 2  # {借款人,贷款} + {孤立实体}
        assert diagnostics["isolated_entity_count"] == 1
        assert diagnostics["isolated_entities"] == ["孤立实体"]
    finally:
        session.close()


def test_graph_diagnostics_sees_this_runs_own_relations_under_autoflush_off(schema, monkeypatch):
    """Production's SessionLocal is autoflush=False (app/database.py), unlike
    the _worker fixture's default-autoflush factory used above. The
    diagnostics query must still see this run's own newly-added entities and
    relations rather than only whatever was committed before this run began."""
    factory = sessionmaker(bind=create_engine(_scoped_url(schema)), autoflush=False)
    session = factory()
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr(
        "app.services.llm_service.extract_ontology",
        lambda *a, **k: {
            "entities": [
                {"name_cn": "借款人", "type": "Concept", "description": "d", "properties": {}},
                {"name_cn": "贷款", "type": "Concept", "description": "d", "properties": {}},
            ],
            "relations": [{"source": "借款人", "target": "贷款", "type": "APPLIES_FOR", "confidence": 0.9}],
            "logic_rules": [], "actions": [],
        },
    )
    extraction_task.run_extraction(task_id)
    try:
        report = session.execute(text(
            "SELECT validation_report FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).scalar_one()
        diagnostics = json.loads(report)["graph_diagnostics"] if isinstance(report, str) else report["graph_diagnostics"]
        assert diagnostics["edge_count"] == 1
        assert diagnostics["connected_components"] == 1
    finally:
        session.close()


_HUB_RESULT = {
    "entities": [
        {"name_cn": "借款人", "type": "Concept", "description": "贷款申请人", "properties": {}},
        {"name_cn": "贷款", "type": "Concept", "description": "d", "properties": {}},
        {"name_cn": "授信额度", "type": "Concept", "description": "d", "properties": {}},
        {"name_cn": "信用评分", "type": "Concept", "description": "d", "properties": {}},
    ],
    "relations": [
        {"source": "借款人", "target": "贷款", "type": "APPLIES_FOR", "confidence": 0.9},
        {"source": "借款人", "target": "授信额度", "type": "HAS", "confidence": 0.9},
        {"source": "借款人", "target": "信用评分", "type": "HAS", "confidence": 0.9},
    ],
    "logic_rules": [], "actions": [],
}


def test_hub_entity_gets_summarized(schema, monkeypatch):
    """A degree-3+ entity (playbook Section V.A hub threshold) gets a
    synthesized profile stored on its properties; low-degree entities do not."""
    factory, session = _worker(schema)
    task_id = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr("app.services.llm_service.extract_ontology", lambda *a, **k: _HUB_RESULT)
    monkeypatch.setattr(
        "app.services.llm_service._call_llm",
        lambda *a, **k: '{"summary": "借款人是核心概念。", "key_facts": ["f1"], '
                        '"time_range": {"start": "2026", "end": "ongoing"}}',
    )
    extraction_task.run_extraction(task_id)
    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id}).mappings().one()
        assert status["status"] == "completed", status["error"]
        rows = session.execute(text(
            "SELECT name_cn, properties FROM entities WHERE ontology_id = 'o-llm'"
        )).mappings().all()
        props_by_name = {r["name_cn"]: (json.loads(r["properties"]) if isinstance(r["properties"], str) else r["properties"]) for r in rows}
        assert props_by_name["借款人"]["summary"] == "借款人是核心概念。"
        assert props_by_name["借款人"]["summary_degree"] == 3
        # a degree-1 entity is not summarized
        assert "summary" not in props_by_name["贷款"]
    finally:
        session.close()


def test_hub_entity_summary_skipped_when_degree_unchanged_on_rerun(schema, monkeypatch):
    """Re-running with the same graph shape must not re-call the summarizer —
    only a changed source-document set (reflected here as a changed degree)
    should trigger re-summarization."""
    factory, session = _worker(schema)
    task_id_1 = _seed(session)
    monkeypatch.setattr("app.database.SessionLocal", factory)
    import app.tasks.extraction as extraction_task
    monkeypatch.setattr("app.services.llm_service.extract_ontology", lambda *a, **k: _HUB_RESULT)
    # resolve_entities isn't exercised on a from-scratch run (nothing existing
    # to resolve against yet), so mocking summarize_entity alone is enough
    # here and avoids also stubbing out the (unrelated) resolution LLM call
    monkeypatch.setattr(
        "app.services.llm_service.summarize_entity",
        lambda *a, **k: {"summary": "first pass", "key_facts": [], "time_range": {"start": "unknown", "end": "unknown"}},
    )
    extraction_task.run_extraction(task_id_1)

    task_id_2 = str(uuid.uuid4())
    session.execute(text(
        "INSERT INTO extraction_tasks (id, ontology_id, prompt_id, model_id, status, parameters, progress, error, created_at, updated_at) "
        "VALUES (:id, 'o-llm', 'p-1', 'm-1', 'queued', :params, '{}'::json, NULL, now(), now())"
    ), {"id": task_id_2, "params": '{"model_name": "mock-extractor", "constraints": []}'})
    session.commit()

    monkeypatch.setattr("app.services.llm_service.extract_ontology", lambda *a, **k: _HUB_RESULT)
    # resolve_entities still runs (4 new entities of the same type) and needs
    # a harmless response so it doesn't attempt a real network call; the
    # assertion this test cares about is that summarize_entity is skipped
    monkeypatch.setattr("app.services.llm_service._call_llm", lambda *a, **k: '{"clusters": []}')
    monkeypatch.setattr(
        "app.services.llm_service.summarize_entity",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("summarizer should not be called again")),
    )
    extraction_task.run_extraction(task_id_2)

    try:
        status = session.execute(text(
            "SELECT status, error FROM extraction_tasks WHERE id = :id"
        ), {"id": task_id_2}).mappings().one()
        assert status["status"] == "completed", status["error"]
        properties = session.execute(text(
            "SELECT properties FROM entities WHERE ontology_id = 'o-llm' AND name_cn = '借款人'"
        )).scalar_one()
        properties = json.loads(properties) if isinstance(properties, str) else properties
        # untouched from the first run — the summarizer was never invoked again
        assert properties["summary"] == "first pass"
    finally:
        session.close()
