"""Deleting an ontology that has entity_instance_relations data must not hit
ONTOLOGY_HAS_DEPENDENT_DATA — the AFTER DELETE trigger on entity_instance_relations
(and entity_instances) re-emits agent_index_outbox rows, which must be swept up
by cleaning agent_index_outbox LAST, not before those tables. Needs a real
Postgres schema (SQLite has none of these triggers)."""
import os
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.routers.ontologies import delete_ontology

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def _scoped_url(schema: str) -> str:
    return f"{TEST_DATABASE_URL}?options={quote(f'-csearch_path={schema},public', safe='-=,')}"


@pytest.fixture
def schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "ontology_delete_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    import subprocess
    import sys
    from pathlib import Path
    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/run_migrations.py", "upgrade", "head"],
        cwd=backend_dir, env=dict(os.environ, DATABASE_URL=_scoped_url(schema)),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    yield schema
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def test_delete_ontology_with_instance_relations_does_not_hit_dependent_data(schema, monkeypatch):
    engine = create_engine(_scoped_url(schema))
    session = sessionmaker(bind=engine)()
    try:
        _run_delete_scenario(session)
    finally:
        # an open transaction would block the fixture's DROP SCHEMA forever
        session.close()
        engine.dispose()


def _run_delete_scenario(session):
    from app.models.user import User
    from app.services.ontology_access import backfill_creator_grants

    oid, u = "o-del-1", "u-del-1"
    session.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,security_domain_id,created_at,updated_at) "
        "VALUES (:u,'d','d@t.com','h','admin',true,:d,now(),now())"
    ), {"u": u, "d": DEFAULT_DOMAIN})
    session.execute(text(
        "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at,security_domain_id,working_revision) "
        "VALUES (:o,'del-test','credit','v1','created',:u,now(),now(),:d,1)"
    ), {"o": oid, "u": u, "d": DEFAULT_DOMAIN})
    session.execute(text(
        "INSERT INTO entities (id, ontology_id, name_cn, type, description, properties, confidence, version, created_at, updated_at) "
        "VALUES ('e-1', :o, '概念', 'Concept', 'd', '{}'::json, 0.9, 'v1', now(), now())"
    ), {"o": oid})
    session.execute(text(
        "INSERT INTO entity_instances (id, entity_id, ontology_id, row_identity, row_data, revision, created_at, updated_at) "
        "VALUES (:i1, 'e-1', :o, 'inst-1', '{}'::json, 1, now(), now()), "
        "(:i2, 'e-1', :o, 'inst-2', '{}'::json, 1, now(), now())"
    ), {"o": oid, "i1": str(uuid.uuid4()), "i2": str(uuid.uuid4())})
    inst_ids = session.execute(text(
        "SELECT id FROM entity_instances WHERE ontology_id = :o ORDER BY row_identity"
    ), {"o": oid}).scalars().all()
    session.execute(text(
        "INSERT INTO relations (id, ontology_id, source_entity, target_entity, type, properties, confidence, created_at) "
        "VALUES ('r-1', :o, 'e-1', 'e-1', 'related_to', '{}'::json, 0.9, now())"
    ), {"o": oid})
    session.execute(text(
        "INSERT INTO entity_instance_relations (id, ontology_id, source_instance_id, target_instance_id, relation_definition_id, properties, revision, created_at, updated_at) "
        "VALUES (:id, :o, :s, :t, 'r-1', '{}'::json, 1, now(), now())"
    ), {"id": str(uuid.uuid4()), "o": oid, "s": inst_ids[0], "t": inst_ids[1]})
    session.commit()

    # confirm the trigger fired on insert too (sanity: outbox has rows before delete)
    outbox_before = session.execute(text(
        "SELECT count(*) FROM agent_index_outbox WHERE ontology_id = :o"
    ), {"o": oid}).scalar_one()
    assert outbox_before > 0

    # rows were inserted via direct SQL, bypassing create_ontology's creator grant
    backfill_creator_grants(session)
    delete_ontology(ontology_id=oid, db=session, current_user=session.get(User, u))

    remaining = session.execute(text(
        "SELECT count(*) FROM ontology_projects WHERE id = :o"
    ), {"o": oid}).scalar_one()
    assert remaining == 0
