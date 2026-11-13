"""LEGACY-SCHEMA: create the legacy runtime tables in the migration chain.

`0001_full_baseline` never created `entity_instances` or `audit_tasks` even
though the ORM registers them; on fresh PostgreSQL the instance reads,
`POST /audit`, and extraction finalization returned 500 ("relation does not
exist").  Revision 0003 now creates both tables with exactly the ORM shapes.

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = Path(__file__).resolve().parents[2]
HELPER = BACKEND_DIR / "alembic_helpers" / "legacy_runtime.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def test_legacy_runtime_red_contract():
    failures = []
    if not HELPER.exists():
        failures.append("alembic_helpers/legacy_runtime.py missing")
    else:
        for name in ("upgrade_legacy_runtime_tables", "downgrade_legacy_runtime_tables"):
            if name not in HELPER.read_text():
                failures.append(f"{name} missing")
    migration_source = (BACKEND_DIR / "alembic" / "versions" / "0003_publication_governance.py").read_text()
    for marker in ("upgrade_legacy_runtime_tables", "downgrade_legacy_runtime_tables"):
        if marker not in migration_source:
            failures.append(f"0003 missing {marker}")
    if failures:
        pytest.fail("RED_LEGACY_RUNTIME_TABLES: " + "; ".join(failures))


def _scoped_url(schema):
    return f"{TEST_DATABASE_URL}?options={quote(f'-csearch_path={schema},public', safe='-=,')}"


def _alembic(schema, *args, check=True):
    return subprocess.run(
        [sys.executable, "scripts/run_migrations.py", *args],
        cwd=BACKEND_DIR,
        env=dict(os.environ, DATABASE_URL=_scoped_url(schema)),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def legacy_schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "legacy_runtime_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield schema, engine
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _orm_columns():
    from sqlalchemy import MetaData

    from app.models import audit_task, entity_instance  # noqa: F401

    metadata = MetaData()
    for table_name in ("entity_instances", "audit_tasks"):
        table = audit_task.Base.metadata.tables[table_name]
        columns = {}
        for column in table.columns:
            columns[column.name] = {
                "type": str(column.type),
                "nullable": column.nullable,
            }
        yield table_name, columns, {index.name: index.unique for index in table.indexes}


# Columns a later revision (0006) adds to entity_instances; the ORM registers
# them, but 0003 must only create the 0003-era shape.
INSTANCE_0006_COLUMNS = {"revision", "deleted_at", "updated_at"}


def test_fresh_0001_to_0003_creates_exact_orm_shapes(legacy_schema):
    schema, engine = legacy_schema
    result = _alembic(schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    migrated = create_engine(_scoped_url(schema))
    inspector = inspect(migrated)
    assert {"entity_instances", "audit_tasks"} <= set(inspector.get_table_names(schema=schema))
    for table_name, orm_columns, orm_indexes in _orm_columns():
        reflected = {column["name"]: column for column in inspector.get_columns(table_name)}
        expected = set(orm_columns)
        if table_name == "entity_instances":
            expected -= INSTANCE_0006_COLUMNS
        assert set(reflected) == expected, table_name
        for name, column in reflected.items():
            assert column["nullable"] == orm_columns[name]["nullable"], (table_name, name)
        reflected_indexes = {index["name"]: index["unique"] for index in inspector.get_indexes(table_name)}
        for index_name, unique in orm_indexes.items():
            assert reflected_indexes.get(index_name) == unique, (table_name, index_name)
    fk_instances = {fk["name"]: fk for fk in inspector.get_foreign_keys("entity_instances")}
    targets = {(fk["constrained_columns"][0], fk["referred_table"], fk["referred_columns"][0], fk["options"].get("ondelete")) for fk in fk_instances.values()}
    assert ("entity_id", "entities", "id", "CASCADE") in targets
    assert ("ontology_id", "ontology_projects", "id", "CASCADE") in targets
    fk_audit = {fk["name"]: fk for fk in inspector.get_foreign_keys("audit_tasks")}
    audit_targets = {(fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete")) for fk in fk_audit.values()}
    assert ("ontology_id", "ontology_projects", "CASCADE") in audit_targets
    assert ("model_id", "model_configs", "SET NULL") in audit_targets
    migrated.dispose()


def test_audit_and_instance_writes_no_longer_500(legacy_schema):
    schema, engine = legacy_schema
    result = _alembic(schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    Session = sessionmaker(bind=create_engine(_scoped_url(schema)))
    with Session() as session:
        session.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at, security_domain_id) "
            "VALUES (:id, 'lr-user', 'lr@example.com', 'hash', 'admin', true, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "domain": DEFAULT_DOMAIN})
        creator_id = session.execute(text("SELECT id FROM users WHERE username='lr-user'")).scalar_one()
        session.execute(text(
            "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at, security_domain_id) "
            "VALUES (:id, 'LR ontology', 'test', 'v0.1', 'draft', :creator, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "creator": creator_id, "domain": DEFAULT_DOMAIN})
        ontology_id = session.execute(text("SELECT id FROM ontology_projects WHERE name='LR ontology'")).scalar_one()
        session.execute(text(
            "INSERT INTO entities (id, ontology_id, name_cn, properties, confidence, version, created_at, updated_at) "
            "VALUES (:id, :o, '实体', '{}'::jsonb, 1.0, 'v0.1', now(), now())"
        ), {"id": str(uuid.uuid4()), "o": ontology_id})
        entity_id = session.execute(text("SELECT id FROM entities WHERE ontology_id=:o"), {"o": ontology_id}).scalar_one()
        session.execute(text(
            "INSERT INTO model_configs (id, name, config_type, provider, models, options, created_by, created_at, updated_at) "
            "VALUES (:id, 'llm', 'llm', 'openai', '[]'::json, '{}'::json, :creator, now(), now())"
        ), {"id": str(uuid.uuid4()), "creator": creator_id})
        model_id = session.execute(text("SELECT id FROM model_configs WHERE name='llm'")).scalar_one()
        session.commit()
        # the extraction/mapping finalization path writes EntityInstance rows
        session.execute(text(
            "INSERT INTO entity_instances (id, entity_id, ontology_id, row_identity, row_data, created_at) "
            "VALUES (:id, :e, :o, 'row-1', '{\"a\": 1}'::json, now())"
        ), {"id": str(uuid.uuid4()), "e": entity_id, "o": ontology_id})
        session.commit()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.deps import get_db
    from app.routers import audit as audit_router
    from app.services.auth_service import create_access_token
    from app.services.ontology_access import backfill_creator_grants

    with Session() as session:
        # the ontology was inserted via direct SQL, bypassing create_ontology's creator grant
        backfill_creator_grants(session)
        actor_id = session.execute(text("SELECT id FROM users WHERE username='lr-user'")).scalar_one()
    token = create_access_token({"sub": actor_id, "role": "admin"})

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(audit_router.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/audit")
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/ontologies/{ontology_id}/audit",
            json={"model_id": model_id, "model_name": "llm"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["task_id"]
    app.dependency_overrides.clear()


def test_0003_downgrade_drops_legacy_runtime_tables(legacy_schema):
    schema, engine = legacy_schema
    result = _alembic(schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    result = _alembic(schema, "downgrade", "0002_entity_identifiers")
    assert result.returncode == 0, result.stderr
    migrated = create_engine(_scoped_url(schema))
    inspector = inspect(migrated)
    assert "entity_instances" not in set(inspector.get_table_names(schema=schema))
    assert "audit_tasks" not in set(inspector.get_table_names(schema=schema))
    migrated.dispose()
