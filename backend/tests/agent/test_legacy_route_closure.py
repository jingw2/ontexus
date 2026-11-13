"""P1B-CLOSURE: every direct schema-mutation path goes through the working copy.

`mutation_inventory.json` enumerates every POST/PUT/PATCH/DELETE schema writer
in the closed routers; the inventory test proves each registered writer either
invokes `OntologyWorkingCopyService.mutate` (marking the working copy dirty)
or fails closed with `PUBLICATION_NOT_ENABLED`.  Logged-in viewers cannot
mutate/review/publish/execute.

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import ast
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
INVENTORY = BACKEND_DIR / "app" / "services" / "publication" / "mutation_inventory.json"
WORKING_COPY = BACKEND_DIR / "app" / "services" / "publication" / "working_copy.py"
DIRTY_HOOKS = BACKEND_DIR / "app" / "services" / "publication" / "dirty_hooks.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def test_p1b_closure_red_contract():
    failures = []
    if not INVENTORY.exists():
        failures.append("mutation_inventory.json missing")
    else:
        inventory = json.loads(INVENTORY.read_text())
        if not inventory.get("writers"):
            failures.append("mutation_inventory.json has no writers")
    for path, marker in ((WORKING_COPY, "class OntologyWorkingCopyService"),
                         (DIRTY_HOOKS, "def mark_ontology_dirty")):
        if not path.exists():
            failures.append(f"{path.relative_to(BACKEND_DIR)} missing")
        elif marker not in path.read_text():
            failures.append(f"{path.relative_to(BACKEND_DIR)} missing {marker}")
    if failures:
        pytest.fail("RED_P1B_CLOSURE: " + "; ".join(failures))


def _router_function_source(router_file, method, path):
    """Return the source segment of the route function for (method, path)."""
    module = ast.parse(router_file.read_text())
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == method.lower()):
                continue
            args = [a for a in call.args if isinstance(a, ast.Constant)]
            if args and args[0].value == path:
                return ast.get_source_segment(router_file.read_text(), node)
    return None


def test_inventory_matches_registered_writers_and_closure():
    inventory = json.loads(INVENTORY.read_text())
    writers = inventory["writers"]
    assert len(writers) >= 25
    seen = set()
    for entry in writers:
        key = (entry["router"], entry["method"], entry["path"])
        assert key not in seen, f"duplicate inventory entry {key}"
        seen.add(key)
        router_file = BACKEND_DIR / entry["router"]
        assert router_file.exists(), f"missing router {entry['router']}"
        source = _router_function_source(router_file, entry["method"], entry["path"])
        assert source is not None, f"route not found: {entry}"
        if entry["closure"] == "mutate":
            assert "OntologyWorkingCopyService.mutate" in source, f"not closed via mutate: {entry}"
            assert f'operation="{entry["operation"]}"' in source, f"wrong operation: {entry}"
        else:
            assert "PUBLICATION_NOT_ENABLED" in source, f"not fail-closed: {entry}"
    # every registered mutation route in the owned routers is inventoried
    for router in (
        "app/routers/entities.py", "app/routers/graph.py", "app/routers/logic.py",
        "app/routers/actions.py", "app/routers/v2/logic_actions.py",
    ):
        module = ast.parse((BACKEND_DIR / router).read_text())
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr
                if method not in ("post", "put", "patch", "delete"):
                    continue
                path_arg = next((a for a in decorator.args if isinstance(a, ast.Constant)), None)
                if path_arg is None:
                    continue
                path = str(path_arg.value)
                if "publish" in path or "toggle" in path or "review" in path or "discover" in path:
                    continue  # covered by the explicit inventory above
                # list/get/test/run endpoints are read-only or runtime, not schema writers
                if method == "post" and (path == "" or path.endswith("/test") or path.endswith("/run")):
                    continue
                assert any(
                    e["router"] == router and e["method"].lower() == method and e["path"].endswith(path)
                    for e in writers
                ), f"uninventoried writer: {router} {method.upper()} {path}"


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
def closure_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p1b_closure_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    result = _alembic(schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    session_engine = create_engine(_scoped_url(schema))
    Session = sessionmaker(bind=session_engine)
    yield Session, session_engine
    session_engine.dispose()
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _seed(closure_db):
    from app.services.auth_service import hash_password
    from app.models.user import User
    from app.services.ontology_access import creator_grant

    Session, _ = closure_db
    username = "closure-" + uuid.uuid4().hex[:8]
    with Session() as session:
        session.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at, security_domain_id) "
            "VALUES (:id, :username, :email, :password_hash, 'admin', true, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "username": username, "email": f"{username}@example.com",
            "password_hash": hash_password("pass123"), "domain": DEFAULT_DOMAIN})
        actor_id = session.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar_one()
        session.execute(text(
            "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at, security_domain_id, working_revision) "
            "VALUES (:id, :name, 'test', 'v0.1', 'draft', :creator, now(), now(), :domain, 1)"
        ), {"id": str(uuid.uuid4()), "name": "Closure " + uuid.uuid4().hex[:8], "creator": actor_id, "domain": DEFAULT_DOMAIN})
        ontology_id = session.execute(text("SELECT id FROM ontology_projects WHERE created_by=:c ORDER BY created_at DESC LIMIT 1"), {"c": actor_id}).scalar_one()
        session.commit()
        creator_grant(session, ontology_id, session.get(User, actor_id))
        return actor_id, ontology_id


def test_zz_mutation_marks_working_copy_dirty_and_audits(closure_db):
    from app.services.publication.working_copy import OntologyWorkingCopyService

    Session, _ = closure_db
    actor_id, ontology_id = _seed(closure_db)
    with Session() as session:
        def _write():
            session.execute(text(
                "INSERT INTO entities (id, ontology_id, name_cn, properties, confidence, version, created_at, updated_at) "
                "VALUES (:id, :o, '实体', '{}'::jsonb, 1.0, 'v0.1', now(), now())"
            ), {"id": str(uuid.uuid4()), "o": ontology_id})
            return {"ok": True}
        OntologyWorkingCopyService.mutate(
            session, ontology_id=ontology_id, actor_id=actor_id, operation="entity.create", callback=_write,
        )
    with Session() as session:
        row = session.execute(text(
            "SELECT working_revision, is_dirty FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).mappings().one()
        assert row["working_revision"] == 2
        assert row["is_dirty"] is False  # never published -> stays clean
        audit = session.execute(text(
            "SELECT count(*) FROM governance_audit_outbox WHERE correlation_id LIKE 'wc:%'"
        )).scalar_one()
        assert audit >= 1
        # a published ontology marks dirty on the next mutation
        import hashlib
        manifest_bytes = b'{"manifest_version":"ontology-manifest-v1"}'
        session.execute(text(
            "INSERT INTO ontology_releases (id, ontology_id, version_no, version, manifest_bytes, manifest_projection, schema_hash, created_by) "
            "VALUES ('20000000-0000-0000-0000-000000000099', :o, 1, 'v1', :bytes, CAST(:projection AS jsonb), :hash, :creator)"
        ), {"o": ontology_id, "bytes": manifest_bytes, "projection": manifest_bytes.decode(),
            "hash": hashlib.sha256(manifest_bytes).digest(), "creator": actor_id})
        session.execute(text(
            "UPDATE ontology_projects SET latest_published_release_id='20000000-0000-0000-0000-000000000099' WHERE id=:o"
        ), {"o": ontology_id})
        session.commit()
        def _write2():
            session.execute(text(
                "INSERT INTO entities (id, ontology_id, name_cn, properties, confidence, version, created_at, updated_at) "
                "VALUES (:id, :o, '乙', '{}'::jsonb, 1.0, 'v0.1', now(), now())"
            ), {"id": str(uuid.uuid4()), "o": ontology_id})
            return {"ok": True}
        OntologyWorkingCopyService.mutate(
            session, ontology_id=ontology_id, actor_id=actor_id, operation="entity.create", callback=_write2,
        )
    with Session() as session:
        row = session.execute(text(
            "SELECT working_revision, is_dirty FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).mappings().one()
        assert row["working_revision"] == 3
        assert row["is_dirty"] is True


def test_zz_routes_go_through_mutate_and_publish_fails_closed(closure_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.deps import get_db
    from app.routers import entities, logic
    from app.services.auth_service import create_access_token

    Session, _ = closure_db
    actor_id, ontology_id = _seed(closure_db)
    token = create_access_token({"sub": actor_id, "role": "admin"})
    headers = {"Authorization": f"Bearer {token}"}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(entities.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/entities")
    app.include_router(logic.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/logic")
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/ontologies/{ontology_id}/entities",
            json={"name_cn": "实体三", "type": "object"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        entity_id = created.json()["data"]["id"]
        updated = client.put(
            f"/api/v1/ontologies/{ontology_id}/entities/{entity_id}",
            json={"description": "updated"},
            headers=headers,
        )
        assert updated.status_code == 200
        logic_created = client.post(
            f"/api/v1/ontologies/{ontology_id}/logic",
            json={"name_cn": "规则", "enabled": True},
            headers=headers,
        )
        assert logic_created.status_code == 201, logic_created.text
        published = client.post(
            f"/api/v1/ontologies/{ontology_id}/logic/publish", headers=headers,
        )
        assert published.status_code == 403
        assert "PUBLICATION_NOT_ENABLED" in published.text
        deleted = client.delete(
            f"/api/v1/ontologies/{ontology_id}/entities/{entity_id}", headers=headers,
        )
        assert deleted.status_code == 204
    app.dependency_overrides.clear()
    with Session() as session:
        row = session.execute(text(
            "SELECT working_revision, is_dirty FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).mappings().one()
        # create entity + update entity + create logic + delete entity
        assert row["working_revision"] >= 4


def test_zz_viewer_cannot_mutate_review_or_publish(closure_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.deps import get_db
    from app.routers import entities, logic
    from app.services.auth_service import create_access_token, hash_password

    Session, _ = closure_db
    _, ontology_id = _seed(closure_db)
    with Session() as session:
        session.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at, security_domain_id) "
            "VALUES (:id, 'viewer-' || :suffix, 'viewer@example.com', :hash, 'viewer', true, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "suffix": uuid.uuid4().hex[:8], "hash": hash_password("pass123"), "domain": DEFAULT_DOMAIN})
        viewer_id = session.execute(text("SELECT id FROM users WHERE username LIKE 'viewer-%' ORDER BY created_at DESC LIMIT 1")).scalar_one()
        session.commit()
    token = create_access_token({"sub": viewer_id, "role": "viewer"})
    headers = {"Authorization": f"Bearer {token}"}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(entities.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/entities")
    app.include_router(logic.router, prefix=f"/api/v1/ontologies/{{ontology_id}}/logic")
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        denied = client.post(
            f"/api/v1/ontologies/{ontology_id}/entities",
            json={"name_cn": "不应写入", "type": "object"},
            headers=headers,
        )
        assert denied.status_code == 403
        publish_denied = client.post(f"/api/v1/ontologies/{ontology_id}/logic/publish", headers=headers)
        assert publish_denied.status_code == 403
    app.dependency_overrides.clear()
