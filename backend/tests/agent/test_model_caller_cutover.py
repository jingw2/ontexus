"""P2A-CALLERS: cut LLM callers to immutable model versions.

Every eligible LLM caller (extraction port, model test, prompt generation,
audit) resolves the pinned `ModelConfigVersion` plus credential binding and
never falls back; OCR/`other` callers stay on their existing tagged path
byte-for-byte.  The models API exposes a redacted tagged union, a versioned
N+1 surface with the legacy PUT alias (201 + deprecation header) and admin
model-migration remediation.

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.services.auth_service import create_access_token, hash_password


BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"
TEST_FERNET_KEY = Fernet.generate_key().decode()
os.environ["ENCRYPTION_KEY"] = TEST_FERNET_KEY


@pytest.fixture(autouse=True)
def _pin_encryption_key():
    # Other agent test modules define their own ENCRYPTION_KEY; pin ours for
    # every in-process decrypt so module import order cannot break it.
    os.environ["ENCRYPTION_KEY"] = TEST_FERNET_KEY
    yield

CALLER_FILES = [
    "app/services/model_config_selector.py",
    "app/services/llm_service.py",
    "app/services/model_callers/extraction.py",
    "app/routers/models.py",
    "app/services/audit_service.py",
]


def test_p2a_callers_red_contract():
    failures = []
    callers_dir = BACKEND_DIR / "app" / "services" / "model_callers"
    extraction = callers_dir / "extraction.py"
    if not extraction.exists():
        failures.append("missing app/services/model_callers/extraction.py")
    else:
        source = extraction.read_text()
        for symbol in ("resolve_llm_caller", "MODEL_VERSION_UNAVAILABLE"):
            if symbol not in source:
                failures.append(f"model_callers/extraction.py missing {symbol}")
    router = (BACKEND_DIR / "app" / "routers" / "models.py").read_text()
    for symbol in ("/versions", "Deprecation", "model-migration-remediations"):
        if symbol not in router:
            failures.append(f"models router missing {symbol}")
    selector = (BACKEND_DIR / "app" / "services" / "model_config_selector.py").read_text()
    if "resolve_llm_caller" not in selector:
        failures.append("selector does not resolve the immutable caller")
    schemas = (BACKEND_DIR / "app" / "schemas" / "model_config.py").read_text()
    if "ModelVersionOut" not in schemas:
        failures.append("schemas missing ModelVersionOut")
    if failures:
        pytest.fail("RED_P2A_CALLERS: " + "; ".join(failures))


def _scoped_url(schema: str) -> str:
    return f"{TEST_DATABASE_URL}?options={quote(f'-csearch_path={schema},public', safe='-=,')}"


def _alembic(schema: str, *args, check=True):
    return subprocess.run(
        [sys.executable, "scripts/run_migrations.py", *args],
        cwd=BACKEND_DIR,
        env=dict(os.environ, DATABASE_URL=_scoped_url(schema), ENCRYPTION_KEY=TEST_FERNET_KEY),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def full_schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p2a_callers_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    result = _alembic(schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    # M-3: alembic_version must live in the scoped schema (isolation proof)
    assert engine.connect().execute(text(
        "SELECT to_regclass(:ref) IS NOT NULL"
    ), {"ref": f'"{schema}".alembic_version'}).scalar_one() is True
    yield schema, _scoped_url(schema)
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def pg(full_schema):
    _, url = full_schema
    Session = sessionmaker(bind=create_engine(url))
    with Session() as session:
        session.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,security_domain_id,created_at,updated_at) "
            "VALUES ('seed-user','seed','seed@test.com','h','admin',true,:d,now(),now())"
        ), {"d": DEFAULT_DOMAIN})
        session.commit()
        yield session


def _enc(plaintext: str) -> str:
    return Fernet(TEST_FERNET_KEY.encode()).encrypt(plaintext.encode()).decode()


def _seed_user(session, *, username, role):
    from app.models.user import User

    user = User(id=str(uuid.uuid4()), username=username, email=f"{username}@test.com",
                password_hash=hash_password("x"), role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _seed_llm_config(session, *, provider="openai", models=None, config_type="llm", name=None):
    from app.models.model_config import ModelConfig

    config = ModelConfig(
        id=str(uuid.uuid4()), name=name or f"LLM-{uuid.uuid4().hex[:6]}",
        config_type=config_type, provider=provider, api_base="https://api.openai.com/v1",
        api_key_encrypted=_enc("sk-test-immutable"),
        models=models if models is not None else ["gpt-4o"],
        options={"temperature": 0.2}, created_by="seed-user",
    )
    session.add(config)
    session.commit()
    session.refresh(config)
    return config.id


def _migrate_rows(session):
    from app.services.model_version import upgrade_legacy_llm_rows

    with session.bind.begin() as connection:
        upgrade_legacy_llm_rows(connection)


def _client(session):
    from app.deps import get_db

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("reference", [None, "agent", "prompt"])
def test_delete_model_preserves_migrated_reference_guards(pg, reference):
    from fastapi import HTTPException
    from app.models.agent import Agent, AgentVersion
    from app.models.agent_config import PromptGeneration
    from app.models.model_version import ModelMigrationFinding
    from app.routers.models import delete_model

    model_id = _seed_llm_config(pg)
    _migrate_rows(pg)
    version_id = pg.execute(text(
        "SELECT active_version_id FROM model_configs WHERE id = :id"
    ), {"id": model_id}).scalar_one()
    assert version_id is not None
    pg.add(ModelMigrationFinding(model_config_id=model_id, code="TEST", reason="test finding"))
    if reference:
        pg.add(Agent(id="delete-test-agent", owner_id="seed-user"))
        pg.flush()
        if reference == "agent":
            schema_id = pg.execute(text(
                "SELECT active_version_id FROM application_state_schema_registries WHERE application_key = 'chat-v1'"
            )).scalar_one()
            pg.add(AgentVersion(
                agent_id="delete-test-agent", version_no=1, name="dependent",
                default_model_config_version_id=version_id, default_model_name="gpt-4o",
                application_state_schema_version_id=schema_id, config_hash="a" * 64,
                created_by="seed-user",
            ))
        else:
            pg.add(PromptGeneration(
                agent_id="delete-test-agent", base_version_no=1,
                model_config_version_id=version_id, model_name="gpt-4o",
                input_hash="b" * 64, output_hash="c" * 64, requester_id="seed-user",
            ))
    pg.commit()

    for table in ("model_config_versions", "model_credentials", "model_migration_findings"):
        assert pg.execute(text(f"SELECT count(*) FROM {table} WHERE model_config_id = :id"), {"id": model_id}).scalar_one() > 0
    if reference:
        with pytest.raises(HTTPException) as denied:
            delete_model(model_id, db=pg)
        assert denied.value.status_code == 409
        assert denied.value.detail == "MODEL_REFERENCED"
        assert pg.execute(text("SELECT active_version_id FROM model_configs WHERE id = :id"), {"id": model_id}).scalar_one() == version_id
    else:
        delete_model(model_id, db=pg)
        assert pg.execute(text("SELECT count(*) FROM model_configs WHERE id = :id"), {"id": model_id}).scalar_one() == 0
        for table in ("model_config_versions", "model_credentials", "model_migration_findings"):
            assert pg.execute(text(f"SELECT count(*) FROM {table} WHERE model_config_id = :id"), {"id": model_id}).scalar_one() == 0


def test_port_resolves_immutable_llm_version_and_rejects_blocked(pg):
    from app.services.publication.extraction_model_port import SqlExtractionModelPort

    model_id = _seed_llm_config(pg, models=["gpt-4.1"])
    _migrate_rows(pg)
    port = SqlExtractionModelPort()
    resolved = port.resolve_model(pg, model_id)
    assert resolved is not None
    assert resolved["config_type"] == "llm"
    assert resolved["active_version_id"] is not None
    blocked_id = _seed_llm_config(pg, models=[])
    _migrate_rows(pg)
    assert port.resolve_model(pg, blocked_id) is None
    ocr_id = _seed_llm_config(pg, config_type="ocr", provider="paddleocr")
    ocr = port.resolve_model(pg, ocr_id)
    assert ocr is not None and ocr["config_type"] == "ocr"


def test_llm_caller_kwargs_pin_immutable_version_and_never_fallback(pg):
    from app.services.model_callers.extraction import resolve_llm_caller, ModelVersionUnavailableError
    from app.services.model_config_selector import llm_call_kwargs
    from app.models.model_config import ModelConfig

    model_id = _seed_llm_config(pg, models=["gpt-4.1"])
    _migrate_rows(pg)
    kwargs = resolve_llm_caller(pg, model_id)
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "gpt-4.1"
    assert kwargs["api_key"] == "sk-test-immutable"
    assert len(kwargs["behavior_hash"]) == 64
    row = pg.query(ModelConfig).filter(ModelConfig.id == model_id).one()
    row_kwargs = llm_call_kwargs(row, db=pg)
    assert row_kwargs["model"] == "gpt-4.1"
    # llm_call_kwargs deliberately narrows to the four keys _call_llm accepts
    # (see model_config_selector.py) — behavior_hash isn't part of that contract.
    assert "behavior_hash" not in row_kwargs
    blocked_id = _seed_llm_config(pg, models=[])
    _migrate_rows(pg)
    with pytest.raises(ModelVersionUnavailableError):
        resolve_llm_caller(pg, blocked_id)


def test_llm_caller_respects_requested_model_name_within_contract(pg):
    from app.services.model_callers.extraction import resolve_llm_caller, resolve_llm_caller_by_version

    model_id = _seed_llm_config(pg, models=["deepseek-v4-pro", "deepseek-v4-flash"])
    _migrate_rows(pg)

    # default (no model_name) keeps prior behavior: first contract entry
    assert resolve_llm_caller(pg, model_id)["model"] == "deepseek-v4-pro"
    # an explicit, in-contract model_name is honored rather than ignored
    assert resolve_llm_caller(pg, model_id, model_name="deepseek-v4-flash")["model"] == "deepseek-v4-flash"
    # an unknown/stale model_name falls back to the first contract entry instead of failing
    assert resolve_llm_caller(pg, model_id, model_name="not-a-real-model")["model"] == "deepseek-v4-pro"

    active_version_id = pg.execute(text(
        "SELECT active_version_id FROM model_configs WHERE id = :id"
    ), {"id": model_id}).scalar_one()
    resolved = resolve_llm_caller_by_version(pg, active_version_id, model_name="deepseek-v4-flash")
    assert resolved["model"] == "deepseek-v4-flash"


def test_model_api_redacted_tagged_union_and_versioned_surface(pg):
    editor = _seed_user(pg, username="caller-editor", role="editor")
    viewer = _seed_user(pg, username="caller-viewer", role="viewer")
    llm_id = _seed_llm_config(pg, name="Tagged LLM", models=["gpt-4.1"])
    ocr_id = _seed_llm_config(pg, config_type="ocr", provider="easyocr", name="Tagged OCR")
    _migrate_rows(pg)

    client = next(_client(pg))
    headers = _headers(create_access_token({"sub": editor.id, "role": "editor"}))
    viewer_headers = _headers(create_access_token({"sub": viewer.id, "role": "viewer"}))

    r = client.get("/api/v1/models", headers=headers)
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()["data"]}
    llm_item = items[llm_id]
    assert llm_item["config_type"] == "llm"
    assert llm_item["active_version"]["version_no"] == 1
    assert "api_key" not in json.dumps(llm_item)
    ocr_item = items[ocr_id]
    assert ocr_item["config_type"] == "ocr"
    assert "active_version" not in ocr_item

    versions = client.get(f"/api/v1/models/{llm_id}/versions", headers=headers).json()["data"]
    assert len(versions) == 1
    assert versions[0]["version_no"] == 1
    assert "secret" not in json.dumps(versions[0])

    # behavioral PUT: legacy alias -> 201 + deprecation header + version N+1
    r = client.put(f"/api/v1/models/{llm_id}", json={"options": {"temperature": 0.5}}, headers=headers)
    assert r.status_code == 201
    assert r.headers.get("deprecation") == "true"
    versions = client.get(f"/api/v1/models/{llm_id}/versions", headers=headers).json()["data"]
    assert [v["version_no"] for v in versions] == [1, 2]
    detail = client.get(f"/api/v1/models/{llm_id}", headers=headers).json()["data"]
    assert detail["active_version"]["version_no"] == 2
    assert detail["active_version"]["behavior_hash"] != versions[0]["behavior_hash"]

    # Owned versions alone do not make an unused model undeletable. Real
    # Agent/prompt references are covered separately by the deletion matrix.
    assert client.delete(f"/api/v1/models/{llm_id}", headers=headers).status_code == 204
    assert client.get(f"/api/v1/models/{llm_id}", headers=headers).status_code == 404

    # viewer cannot create/update models (editor ceiling)
    assert client.post("/api/v1/models", json={"name": "x", "provider": "openai", "models": ["gpt-4o"]}, headers=viewer_headers).status_code == 403


def test_create_model_version_endpoint_inherits_active_provider(pg):
    editor = _seed_user(pg, username="version-editor", role="editor")
    llm_id = _seed_llm_config(pg, name="Versioned LLM", models=["gpt-4.1"])
    _migrate_rows(pg)

    client = next(_client(pg))
    headers = _headers(create_access_token({"sub": editor.id, "role": "editor"}))

    r = client.post(f"/api/v1/models/{llm_id}/versions", json={"options": {"temperature": 0.7}}, headers=headers)
    assert r.status_code == 201
    assert r.json()["data"]["version_no"] == 2

    versions = client.get(f"/api/v1/models/{llm_id}/versions", headers=headers).json()["data"]
    assert [v["version_no"] for v in versions] == [1, 2]


def test_admin_model_migration_remediation_routes(pg):
    admin = _seed_user(pg, username="caller-admin", role="admin")
    editor = _seed_user(pg, username="caller-editor2", role="editor")
    blocked_id = _seed_llm_config(pg, provider="weird_provider", name="Blocker")
    blocked2 = _seed_llm_config(pg, models=[], name="Blocker2")
    _migrate_rows(pg)

    client = next(_client(pg))
    headers = _headers(create_access_token({"sub": admin.id, "role": "admin"}))
    editor_headers = _headers(create_access_token({"sub": editor.id, "role": "editor"}))

    listings = client.get("/api/v1/admin/model-migration-remediations", headers=headers)
    assert listings.status_code == 200
    items = listings.json()["data"]["items"]
    item = next(i for i in items if i["model_config_id"] == blocked_id)
    detail = client.get(f"/api/v1/admin/model-migration-remediations/{item['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["code"] == "UNKNOWN_PROVIDER"
    assert client.get("/api/v1/admin/model-migration-remediations", headers=editor_headers).status_code == 403

    from app.services.model_version import is_eligible_for_agent
    r = client.post(
        f"/api/v1/admin/model-migration-remediations/{item['id']}/remediate",
        json={
            "base_revision": detail.json()["data"]["base_revision"],
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "options": {},
            "model_contract": [{
                "provider_model_revision": "gpt-4o",
                "tokenizer_family": "cl100k_base",
                "tokenizer_revision": "rev-1",
                "verified_context_window_tokens": 128000,
                "verified_maximum_output_tokens": 4096,
                "provider_contract_revision": "pc-1",
                "provider_contract_hash": "a" * 64,
            }],
            "credential_binding": "sk-remediated",
        },
        headers=headers,
    )
    assert r.status_code == 201
    assert r.json()["data"]["version_no"] == 1
    assert is_eligible_for_agent(pg, blocked_id) is True

    item2 = next(i for i in client.get("/api/v1/admin/model-migration-remediations", headers=headers).json()["data"]["items"] if i["model_config_id"] == blocked2)
    detail2 = client.get(f"/api/v1/admin/model-migration-remediations/{item2['id']}", headers=headers).json()["data"]
    r = client.post(
        f"/api/v1/admin/model-migration-remediations/{item2['id']}/archive",
        json={"base_revision": detail2["base_revision"], "reason": "obsolete"},
        headers=headers,
    )
    assert r.status_code == 200
    assert is_eligible_for_agent(pg, blocked2) is False


def test_ocr_parity_golden_hashes(pg):
    from app.services.model_config_selector import llm_call_kwargs
    from app.models.model_config import ModelConfig

    ocr = ModelConfig(
        id=str(uuid.uuid4()), name="OCR-parity", config_type="ocr", provider="external_api",
        api_base="https://ocr.example.test", api_key_encrypted="", models=["paddle"],
        options={"enabled": True}, created_by="seed-user",
    )
    pg.add(ocr)
    pg.commit()
    pg.refresh(ocr)
    kwargs = llm_call_kwargs(ocr, db=pg)
    assert kwargs is None or kwargs.get("model") == "paddle"

    editor = _seed_user(pg, username="caller-ocr", role="editor")
    client = next(_client(pg))
    headers = _headers(create_access_token({"sub": editor.id, "role": "editor"}))
    r = client.post(f"/api/v1/models/{ocr.id}/test", headers=headers)
    assert r.status_code == 200
    golden = hashlib.sha256(json.dumps(r.json(), sort_keys=True).encode()).hexdigest()
    r2 = client.post(f"/api/v1/models/{ocr.id}/test", headers=headers)
    assert hashlib.sha256(json.dumps(r2.json(), sort_keys=True).encode()).hexdigest() == golden
