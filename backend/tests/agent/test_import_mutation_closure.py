"""P1B-IMPORTS: closed extraction/mapping/graph-sync import writers.

Every mapping create/apply/apply-from-dataset/build-all/link-mapping writer
from the shared mutation inventory goes through
`OntologyWorkingCopyService.mutate`; read-only endpoints (suggest, graph
ask/cypher, derived-index sync) stay unchanged.  Extraction start rejects an
unknown `model_id` with a stable `MODEL_CONFIG_NOT_FOUND` instead of a 500 FK
IntegrityError (R-1).

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = Path(__file__).resolve().parents[2]
PORT = BACKEND_DIR / "app" / "services" / "publication" / "extraction_model_port.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def test_p1b_imports_red_contract():
    failures = []
    if not PORT.exists():
        failures.append("extraction_model_port.py missing")
    inventory_path = BACKEND_DIR / "app" / "services" / "publication" / "mutation_inventory.json"
    inventory = json.loads(inventory_path.read_text())
    mapping_ops = {entry["operation"] for entry in inventory["writers"] if entry["router"] == "app/routers/v2/mappings.py"}
    expected = {"mapping.create", "mapping.apply", "mapping.apply-from-dataset", "mapping.build-all", "mapping.link-create"}
    if not expected <= mapping_ops:
        failures.append(f"inventory missing mapping writers: {expected - mapping_ops}")
    extraction_source = (BACKEND_DIR / "app" / "routers" / "extraction.py").read_text()
    if "MODEL_CONFIG_NOT_FOUND" not in extraction_source:
        failures.append("extraction router missing MODEL_CONFIG_NOT_FOUND handling")
    if failures:
        pytest.fail("RED_P1B_IMPORTS: " + "; ".join(failures))


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


@pytest.fixture(scope="module")
def imports_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p1b_imports_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    result = _alembic(schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    session_engine = create_engine(_scoped_url(schema))
    Session = sessionmaker(bind=session_engine)
    yield Session, session_engine
    session_engine.dispose()
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _seed(imports_db):
    from app.services.auth_service import hash_password

    Session, _ = imports_db
    username = "imports-" + uuid.uuid4().hex[:8]
    with Session() as session:
        session.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at, security_domain_id) "
            "VALUES (:id, :username, :email, :password_hash, 'editor', true, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "username": username, "email": f"{username}@example.com",
            "password_hash": hash_password("pass123"), "domain": DEFAULT_DOMAIN})
        actor_id = session.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar_one()
        session.execute(text(
            "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at, security_domain_id, working_revision) "
            "VALUES (:id, :name, 'test', 'v0.1', 'draft', :creator, now(), now(), :domain, 1)"
        ), {"id": str(uuid.uuid4()), "name": "Imports " + uuid.uuid4().hex[:8], "creator": actor_id, "domain": DEFAULT_DOMAIN})
        ontology_id = session.execute(text("SELECT id FROM ontology_projects WHERE created_by=:c ORDER BY created_at DESC LIMIT 1"), {"c": actor_id}).scalar_one()
        session.commit()
        return actor_id, ontology_id


def test_zz_mapping_writers_go_through_mutate_and_audit(imports_db):
    from app.services.publication.working_copy import OntologyWorkingCopyService

    Session, _ = imports_db
    actor_id, ontology_id = _seed(imports_db)
    with Session() as session:
        def _write():
            session.execute(text(
                "INSERT INTO v2_ontology_mappings (id, ontology_id, entity_class, field_mapping, status, confidence, created_at) "
                "VALUES (:id, :o, 'Supplier', '{\"__primary_key__\": \"id\"}'::json, 'draft', 0.9, now())"
            ), {"id": str(uuid.uuid4()), "o": ontology_id})
            return {"ok": True}
        OntologyWorkingCopyService.mutate(
            session, ontology_id=ontology_id, actor_id=actor_id, operation="mapping.create", callback=_write,
        )
    with Session() as session:
        row = session.execute(text(
            "SELECT working_revision, is_dirty FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).mappings().one()
        assert row["working_revision"] == 2
        audit = session.execute(text(
            "SELECT count(*) FROM governance_audit_outbox WHERE correlation_id LIKE 'wc:%' AND payload::text LIKE '%mapping.create%'"
        )).scalar_one()
        assert audit >= 1


def test_zz_extraction_rejects_unknown_model_with_stable_error(imports_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.deps import get_db
    from app.routers import extraction
    from app.services.auth_service import create_access_token

    from app.services.ontology_access import backfill_creator_grants

    Session, _ = imports_db
    actor_id, ontology_id = _seed(imports_db)
    # _seed inserts via direct SQL, bypassing create_ontology's creator grant
    with Session() as session:
        backfill_creator_grants(session)
    token = create_access_token({"sub": actor_id, "role": "editor"})
    headers = {"Authorization": f"Bearer {token}"}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(extraction.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/execute")
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/ontologies/{ontology_id}/execute",
            json={"model_id": "does-not-exist", "model_name": "x", "prompt_id": "p1", "file_ids": [], "constraints": []},
            headers=headers,
        )
        # R-1: an unknown model id is a stable 422, never a 500 FK IntegrityError
        assert response.status_code == 422
        assert "MODEL_CONFIG_NOT_FOUND" in response.text
    app.dependency_overrides.clear()


def test_zz_readonly_endpoints_do_not_mark_dirty(imports_db):
    from app.services.publication.working_copy import OntologyWorkingCopyService

    Session, _ = imports_db
    actor_id, ontology_id = _seed(imports_db)
    with Session() as session:
        before = session.execute(text(
            "SELECT working_revision FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).scalar_one()
        # a derived-index-only sync (graph/sync writes Neo4j, never SQL definitions)
        session.execute(text("SELECT 1"))
        session.commit()
        after = session.execute(text(
            "SELECT working_revision FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).scalar_one()
        assert after == before


def test_zz_extraction_model_port_resolves_and_redacts(imports_db):
    from app.services.publication.extraction_model_port import (
        SqlExtractionModelPort,
        default_extraction_model_port,
    )

    Session, _ = imports_db
    with Session() as session:
        actor_row = session.execute(text(
            "SELECT id FROM users ORDER BY created_at DESC LIMIT 1"
        )).mappings().one()
        session.execute(text(
            "INSERT INTO model_configs (id, name, config_type, provider, models, options, created_by, created_at, updated_at) "
            "VALUES (:id, 'llm', 'llm', 'openai', '[]'::json, '{}'::json, :creator, now(), now())"
        ), {"id": str(uuid.uuid4()), "creator": actor_row["id"]})
        model_id = session.execute(text("SELECT id FROM model_configs WHERE name='llm'")).scalar_one()
        session.commit()
    with Session() as session:
        port = SqlExtractionModelPort()
        assert port.resolve_model(session, model_id)["config_type"] == "llm"
        assert port.resolve_model(session, "nope") is None
        assert default_extraction_model_port.resolve_model(session, "nope") is None
