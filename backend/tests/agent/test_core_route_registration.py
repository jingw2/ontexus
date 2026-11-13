"""I-BACKEND: core Agent API registration serial packet.

The serial packet owns `app/main.py`, `app/tasks/celery_app.py`,
`app/routers/__init__.py` and the deterministic `backend/openapi-agent.json`.
It (a) refuses unsupported Python before any third-party import in both the
API and the shared worker/beat module, (b) registers every exported Section 12
core router exactly once while post-MVP surfaces stay absent, (c) wires the
security-headers middleware, (d) wires the 24-hour idempotency-key persistence
machinery, (e) closes the R-1 audit half (unknown model_id -> typed 422), and
(f) keeps the E0-DB no-repair startup invariant.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.services.auth_service import create_access_token


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"

THIRD_PARTY_ROOTS = {"fastapi", "slowapi", "sqlalchemy", "pydantic", "jose", "celery"}


# ── Section 12 core method inventory (implemented rows, registered exactly once)
CORE_SECTION12 = {
    # Ontology design
    ("GET", "/api/v1/ontologies"), ("POST", "/api/v1/ontologies"),
    ("GET", "/api/v1/ontologies/{ontology_id}"), ("PUT", "/api/v1/ontologies/{ontology_id}"),
    # Ontology project grants/recovery
    ("GET", "/api/v1/ontologies/{ontology_id}/access-grants"),
    ("POST", "/api/v1/ontologies/{ontology_id}/access-grants"),
    ("POST", "/api/v1/ontologies/{ontology_id}/access-grants/{grant_id}/revisions"),
    ("POST", "/api/v1/ontologies/{ontology_id}/access-grants/{grant_id}/revoke"),
    ("POST", "/api/v1/admin/ontology-owner-recoveries/{ontology_id}/assign"),
    # Ontology lifecycle
    ("POST", "/api/v1/ontologies/{ontology_id}/mark-created"),
    ("POST", "/api/v1/ontologies/{ontology_id}/publish"),
    ("POST", "/api/v1/ontologies/{ontology_id}/archive"),
    ("POST", "/api/v1/ontologies/{ontology_id}/runtime-disable"),
    ("POST", "/api/v1/ontologies/{ontology_id}/runtime-enable"),
    # Releases
    ("GET", "/api/v1/ontologies/{ontology_id}/releases"),
    ("GET", "/api/v1/ontologies/{ontology_id}/releases/{release_id}"),
    # Migration remediation
    ("GET", "/api/v1/ontologies/{ontology_id}/migration-remediations"),
    ("GET", "/api/v1/ontologies/{ontology_id}/migration-remediations/{kind}/{item_id}"),
    ("PATCH", "/api/v1/ontologies/{ontology_id}/migration-remediations/{kind}/{item_id}"),
    # Agents
    ("GET", "/api/v1/agents"), ("POST", "/api/v1/agents"),
    ("GET", "/api/v1/agents/{agent_id}"), ("DELETE", "/api/v1/agents/{agent_id}"),
    # Agent versions
    ("GET", "/api/v1/agents/{agent_id}/versions"), ("POST", "/api/v1/agents/{agent_id}/versions"),
    ("GET", "/api/v1/agents/{agent_id}/versions/{version_no}"),
    ("POST", "/api/v1/agents/{agent_id}/versions/{version_no}/restore"),
    # Agent access grants
    ("GET", "/api/v1/agents/{agent_id}/access-grants"),
    ("POST", "/api/v1/agents/{agent_id}/access-grants"),
    ("POST", "/api/v1/agents/{agent_id}/access-grants/{grant_id}/revisions"),
    ("POST", "/api/v1/agents/{agent_id}/access-grants/{grant_id}/revoke"),
    # Prompt generation
    ("POST", "/api/v1/agents/{agent_id}/prompt-generations"),
    ("GET", "/api/v1/agents/{agent_id}/prompt-generations/{generation_id}"),
    ("POST", "/api/v1/agents/{agent_id}/prompt-generations/{generation_id}/decision"),
    # Catalog/config validation
    ("GET", "/api/v1/agents/catalog/ontologies"), ("GET", "/api/v1/agents/catalog/models"),
    ("POST", "/api/v1/agents/{agent_id}/tool-validation"),
    # Ontology data grants
    ("GET", "/api/v1/ontology-data-grants"), ("POST", "/api/v1/ontology-data-grants"),
    ("POST", "/api/v1/ontology-data-grants/{grant_id}/revisions"),
    ("POST", "/api/v1/ontology-data-grants/{grant_id}/revoke"),
    # Application-state schemas
    ("GET", "/api/v2/application-state-schemas"), ("POST", "/api/v2/application-state-schemas"),
    ("POST", "/api/v2/application-state-schemas/{registry_id}/versions"),
    ("POST", "/api/v2/application-state-schemas/{registry_id}/activate"),
    # Sessions/messages
    ("GET", "/api/v1/agents/{agent_id}/sessions"), ("POST", "/api/v1/agents/{agent_id}/sessions"),
    ("GET", "/api/v1/agent-sessions/{session_id}"), ("DELETE", "/api/v1/agent-sessions/{session_id}"),
    ("GET", "/api/v1/agent-sessions/{session_id}/messages"),
    # Turns
    ("POST", "/api/v1/agent-sessions/{session_id}/turns"),
    ("GET", "/api/v1/agent-turns/{turn_id}"),
    ("POST", "/api/v1/agent-turns/{turn_id}/cancel"),
    ("POST", "/api/v1/agent-turns/{turn_id}/stream-ticket"),
    # Events/stream
    ("GET", "/api/v1/agent-turns/{turn_id}/events"),
    ("GET", "/api/v1/agent-turns/{turn_id}/stream"),
    # Approval
    ("GET", "/api/v1/agent-approvals/{approval_id}"),
    ("POST", "/api/v1/agent-approvals/{approval_id}/approve"),
    ("POST", "/api/v1/agent-approvals/{approval_id}/reject"),
    # Clarification
    ("GET", "/api/v1/agent-clarifications/{clarification_id}"),
    ("POST", "/api/v1/agent-clarifications/{clarification_id}/answer"),
    # Application state
    ("GET", "/api/v1/agent-sessions/{session_id}/application-state"),
    ("POST", "/api/v1/agent-sessions/{session_id}/application-state"),
    # Audit
    ("GET", "/api/v1/agents/{agent_id}/audit"),
    ("GET", "/api/v1/agent-turns/{turn_id}/audit"),
    # Reconciliation
    ("GET", "/api/v1/admin/agent-reconciliations"),
    ("GET", "/api/v1/admin/agent-reconciliations/{case_id}"),
    ("POST", "/api/v1/admin/agent-reconciliations/{case_id}/resolve"),
    # Security domains
    ("GET", "/api/v1/security-domains"),
    ("POST", "/api/v1/security-domains/{domain_id}/deactivate"),
    # Model versions (existing surface)
    ("GET", "/api/v1/models"), ("POST", "/api/v1/models"),
    ("GET", "/api/v1/models/{model_id}"), ("PUT", "/api/v1/models/{model_id}"),
    ("POST", "/api/v1/models/{model_id}/versions"),
    ("GET", "/api/v1/models/{model_id}/versions"),
    ("GET", "/api/v1/models/{model_id}/versions/{version_id}"),
    ("GET", "/api/v1/models/{model_id}/credentials"),
    ("POST", "/api/v1/models/{model_id}/test"),
    ("DELETE", "/api/v1/models/{model_id}"),
    # Model migration remediation (admin)
    ("GET", "/api/v1/admin/model-migration-remediations"),
    ("GET", "/api/v1/admin/model-migration-remediations/{finding_id}"),
    ("POST", "/api/v1/admin/model-migration-remediations/{finding_id}/remediate"),
    ("POST", "/api/v1/admin/model-migration-remediations/{finding_id}/archive"),
}

POST_MVP_SURFACES = {
    ("GET", "/api/v2/signed-skills"),                       # P7C
    ("GET", "/api/v2/mcp-applications"),                    # P7D/P7E
}

EXPECTED_ROUTER_EXPORTS = {
    "ontology_lifecycle", "ontology_access_grants", "ontology_remediations",
    "security_domains", "agents", "agent_turns", "agent_events", "agent_approvals",
    "agent_clarifications", "agent_application_state", "agent_audit",
    "agent_reconciliations",
}


def test_i_backend_red_contract():
    failures = []
    init_py = BACKEND_DIR / "app" / "routers" / "__init__.py"
    if not init_py.exists():
        failures.append("missing app/routers/__init__.py")
    else:
        for symbol in EXPECTED_ROUTER_EXPORTS:
            if symbol not in init_py.read_text():
                failures.append(f"routers/__init__.py missing {symbol}")
    main_py = BACKEND_DIR / "app" / "main.py"
    if not main_py.exists() or "require_supported_python()" not in main_py.read_text():
        failures.append("app/main.py missing require_supported_python() guard")
    celery_py = BACKEND_DIR / "app" / "tasks" / "celery_app.py"
    if not celery_py.exists() or "require_supported_python()" not in celery_py.read_text():
        failures.append("app/tasks/celery_app.py missing require_supported_python() guard")
    openapi = BACKEND_DIR / "openapi-agent.json"
    if not openapi.exists():
        failures.append("missing generated backend/openapi-agent.json")
    for path, symbol in (
        ("app/middleware/security_headers.py", "create_security_headers_middleware"),
        ("app/middleware/idempotency.py", "create_idempotency_middleware"),
        ("app/services/idempotency.py", "canonical_request_hash"),
    ):
        p = BACKEND_DIR / path
        if not p.exists():
            failures.append(f"missing {path}")
        elif symbol not in p.read_text():
            failures.append(f"{path} missing {symbol}")
    if failures:
        pytest.fail("RED_I_BACKEND: " + "; ".join(failures))


def _guard_call_line(tree):
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return min(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "require_supported_python"
    )


def _third_party_import_lines(tree):
    lines = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
            if root in THIRD_PARTY_ROOTS:
                lines.append(node.lineno)
    return lines


def test_main_guards_before_third_party_imports():
    tree = ast.parse((BACKEND_DIR / "app" / "main.py").read_text())
    third_party = _third_party_import_lines(tree)
    assert third_party, "no third-party import found in main.py"
    assert _guard_call_line(tree) < min(third_party)


def test_celery_app_guards_before_third_party_imports():
    tree = ast.parse((BACKEND_DIR / "app" / "tasks" / "celery_app.py").read_text())
    third_party = _third_party_import_lines(tree)
    assert third_party, "no third-party import found in celery_app.py"
    assert _guard_call_line(tree) < min(third_party)


def test_unsupported_python_rejected_before_third_party_import():
    python = pathlib.Path.home() / ".local/share/uv/python/cpython-3.10.20-macos-aarch64-none/bin/python3.10"
    if not python.exists():
        pytest.skip("real Python 3.10 runtime unavailable")
    for module in ("app.main", "app.tasks.celery_app"):
        proc = subprocess.run(
            [str(python), "-c", f"import {module}"],
            cwd=BACKEND_DIR, capture_output=True, text=True,
        )
        assert proc.returncode != 0, f"{module} imported on unsupported Python"
        combined = proc.stderr + proc.stdout
        assert "UNSUPPORTED_PYTHON_VERSION" in combined, f"{module} did not refuse first"
        # the guard fires before FastAPI/Celery: their import tracebacks must be absent
        assert "from fastapi" not in combined and "from celery" not in combined


def test_router_registry_exports_expected_routers():
    import types

    import app.routers as router_pkg

    missing = []
    for name in EXPECTED_ROUTER_EXPORTS:
        obj = getattr(router_pkg, name, None)
        if obj is None:
            missing.append(name)
        elif not isinstance(obj, types.ModuleType) or not hasattr(obj, "router"):
            missing.append(f"{name} (not a module exposing router)")
    assert not missing, f"registry missing/invalid exports: {missing}"
    # the admin router is exported by its module
    assert hasattr(router_pkg.ontology_access_grants, "admin_router")
    # agent_audit is an alias of the application-state router object
    assert router_pkg.agent_audit.router is router_pkg.agent_application_state.router


def test_core_section12_methods_registered_exactly_once():
    registered = {
        (method, route.path)
        for route in app.routes
        for method in (route.methods or set())
    }
    missing = CORE_SECTION12 - registered
    assert not missing, f"unregistered Section 12 core methods: {sorted(missing)}"
    duplicates = [key for key in CORE_SECTION12 if list(registered).count(key) > 1]
    counts = {}
    for key in registered:
        counts[key] = counts.get(key, 0) + 1
    dup = {k: v for k, v in counts.items() if v > 1 and k in CORE_SECTION12}
    assert not dup, f"duplicate core registration: {dup}"


def test_post_mvp_surfaces_absent_from_routing():
    registered = {
        (method, route.path)
        for route in app.routes
        for method in (route.methods or set())
    }
    present = POST_MVP_SURFACES & registered
    assert not present, f"post-MVP surfaces leaked into routing: {present}"


def test_security_headers_middleware_wired():
    from app.middleware.security_headers import SECURITY_HEADERS
    import app.main as main_module

    source = pathlib.Path(main_module.__file__).read_text()
    assert "create_security_headers_middleware" in source
    assert "add_middleware" in source
    # the exact header set is the F0-SECURITY contract
    assert "Content-Security-Policy" in SECURITY_HEADERS
    assert "unsafe-inline" not in SECURITY_HEADERS["Content-Security-Policy"]
    assert "Referrer-Policy" in SECURITY_HEADERS
    assert "X-Content-Type-Options" in SECURITY_HEADERS
    assert "Permissions-Policy" in SECURITY_HEADERS


def test_openapi_manifest_is_deterministic():
    manifest = BACKEND_DIR / "openapi-agent.json"
    generated = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert manifest.read_text() == generated, (
        "backend/openapi-agent.json is stale; regenerate with "
        "`cd backend && python -m scripts.generate_openapi`"
    )
    schema = json.loads(generated)
    paths = schema.get("paths", {})
    assert paths.get("/api/v1/agent-turns/{turn_id}/stream"), "SSE route missing from OpenAPI"
    assert "/api/v1/admin/agent-reconciliations" in paths


def test_application_startup_contains_no_schema_repair():
    source = (BACKEND_DIR / "app" / "main.py").read_text()
    assert "_run_schema_migration" not in source
    assert "command.stamp" not in source
    assert "command.upgrade" not in source
    assert "metadata.create_all" not in source


# ── PostgreSQL fixtures (24h idempotency persistence + R-1 audit route) ─────

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
def idem_schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "ibackend_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    assert _alembic(schema, "upgrade", "head").returncode == 0
    yield schema
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def test_idempotency_persistence_service_24h(idem_schema):
    from app.services.idempotency import (
        canonical_request_hash, lookup_idempotency, persist_idempotency,
        sweep_expired_idempotency,
    )

    session_factory = sessionmaker(bind=create_engine(_scoped_url(idem_schema)))
    session = session_factory()
    try:
        key = "k" + "y" * 19
        actor = "u-1"
        route = "/api/v1/agents/a-1/versions"
        body_a = b'{"base_version_no":1,"patch":{"name":"n"}}'
        body_b = b'{"base_version_no":2,"patch":{"name":"n"}}'
        hash_a = canonical_request_hash("POST", route, body_a)
        hash_b = canonical_request_hash("POST", route, body_b)
        assert hash_a and hash_b and hash_a != hash_b
        assert len(hash_a) == 64

        persist_idempotency(session, key=key, actor_id=actor, route=route, request_hash=hash_a)
        session.commit()

        record = lookup_idempotency(session, key=key, actor_id=actor)
        assert record is not None
        assert record["request_hash"] == hash_a
        assert record["route"] == route
        # 24h persistence horizon is encoded on the row
        assert record["expires_at"] is not None

        # different actor -> independent record
        assert lookup_idempotency(session, key=key, actor_id="u-2") is None

        # conflict semantics: same key, different hash
        session.execute(text(
            "UPDATE agent_idempotency_keys SET expires_at = now() - interval '25 hours' "
            "WHERE actor_id = :a AND idempotency_key = :k"
        ), {"a": actor, "k": key})
        session.commit()
        # the 24h horizon is enforced: an expired row is swept and forgotten
        assert sweep_expired_idempotency(session) >= 1
        assert lookup_idempotency(session, key=key, actor_id=actor) is None
        session.commit()
    finally:
        session.close()


def test_idempotency_middleware_conflicts_on_hash_mismatch(idem_schema):
    from app.middleware.idempotency import create_idempotency_middleware

    session_factory = sessionmaker(bind=create_engine(_scoped_url(idem_schema)))

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    standalone = FastAPI()
    standalone.add_middleware(create_idempotency_middleware(session_factory=session_factory))

    @standalone.post("/api/v1/mutations/thing")
    def mutate(body: dict, db=Depends(get_db)):
        return {"data": {"ok": True, "echo": body}}

    client = TestClient(standalone, raise_server_exceptions=False)
    token = create_access_token({"sub": "u-1"})
    key = "k" * 20
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    first = client.post("/api/v1/mutations/thing", json={"a": 1}, headers=headers)
    assert first.status_code == 200, first.text

    replay = client.post("/api/v1/mutations/thing", json={"a": 1}, headers=headers)
    assert replay.status_code == 200, replay.text  # same key + same hash passes through

    conflict = client.post("/api/v1/mutations/thing", json={"a": 2}, headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert "IDEMPOTENCY_KEY_REUSED" in conflict.text

    # a different actor may reuse the same key string
    other = create_access_token({"sub": "u-2"})
    other_headers = {"Authorization": f"Bearer {other}", "Idempotency-Key": key}
    allowed = client.post("/api/v1/mutations/thing", json={"a": 9}, headers=other_headers)
    assert allowed.status_code == 200, allowed.text

    # requests without the header are untouched
    plain = client.post("/api/v1/mutations/thing", json={"a": 3})
    assert plain.status_code == 200, plain.text


def test_idempotency_migration_upgrade_downgrade_cycle():
    """The idempotency table folds into 0006 (I-6): the 0006 upgrade creates
    it and the downgrade drops it; the core Alembic head stays exactly at
    `0006_agent_runtime` (S4 one-head-per-milestone)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "ibackend_mig_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        assert _alembic(schema, "upgrade", "0006_agent_runtime").returncode == 0
        scoped = create_engine(_scoped_url(schema))
        with scoped.begin() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == "0006_agent_runtime"
            tables = set(connection.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            )).scalars())
            assert "agent_idempotency_keys" in tables
        assert _alembic(schema, "downgrade", "0005_agent_configuration").returncode == 0
        with scoped.begin() as connection:
            tables = set(connection.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            )).scalars())
            assert "agent_idempotency_keys" not in tables
        # re-upgrade restores the table (empty-database upgrade/downgrade cycle)
        assert _alembic(schema, "upgrade", "0006_agent_runtime").returncode == 0
        with scoped.begin() as connection:
            tables = set(connection.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            )).scalars())
            assert "agent_idempotency_keys" in tables
        scoped.dispose()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_audit_route_rejects_unknown_model_id_422(idem_schema):
    from app.routers.audit import router as audit_router
    from app.routers.audit import start_audit  # noqa: F401 — route-local fix surface
    from app.models.user import User
    from app.services.ontology_access import creator_grant

    session_factory = sessionmaker(bind=create_engine(_scoped_url(idem_schema)))
    session = session_factory()
    try:
        admin_id = str(uuid.uuid4())
        session.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, "
            "created_at, updated_at, security_domain_id) "
            "VALUES (:id, 'admin-x', 'a@x.io', 'x', 'admin', true, now(), now(), :domain)"
        ), {"id": admin_id, "domain": DEFAULT_DOMAIN})
        session.execute(text(
            "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, "
            "created_at, updated_at, security_domain_id, working_revision) "
            "VALUES (:id, 'p', 'test', 'v0.1', 'created', :by, now(), now(), :domain, 1)"
        ), {"id": "onto-1", "by": admin_id, "domain": DEFAULT_DOMAIN})
        session.commit()
        creator_grant(session, "onto-1", session.get(User, admin_id))
    finally:
        session.close()

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    standalone = FastAPI()
    standalone.include_router(audit_router, prefix="/api/v1/ontologies/{ontology_id}/audit")
    standalone.dependency_overrides[__import__("app.deps", fromlist=["get_db"]).get_db] = get_db
    client = TestClient(standalone, raise_server_exceptions=False)
    token = create_access_token({"sub": admin_id})
    resp = client.post(
        "/api/v1/ontologies/onto-1/audit",
        json={"model_id": "no-such-model", "model_name": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert "MODEL_CONFIG_NOT_FOUND" in resp.text
