"""P1A-INTEGRATE: single hidden 0003 revision and domain-aware roots.

Revision `0003_publication_governance` must call every upstream named upgrade
helper exactly once in the normative order (domain, release, audit, identity)
and every downgrade helper exactly once in strict reverse order.  The User and
OntologyProject ORM roots become non-null domain-aware mappings, and
`load_all_models()` registers exactly the implemented upstream models.  The
test harness establishes a disposable verified-0003 database before importing
`app.main` and never relies on startup create-all/stamp repair.

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import importlib.util
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
MIGRATION = BACKEND_DIR / "alembic" / "versions" / "0003_publication_governance.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"
NEW_0003_TABLES = {
    "security_domains",
    "auth_refresh_families",
    "auth_refresh_tokens",
    "ontology_releases",
    "governance_audit_logs",
    "governance_audit_outbox",
    "governance_audit_chain_heads",
    "entity_property_definitions",
    "ontology_migration_findings",
    "ontology_project_access_grants",
    "entity_instances",
    "audit_tasks",
}
# Every modeled table now exists in the 0003 migrated schema: the
# LEGACY-SCHEMA mini-packet added entity_instances and audit_tasks, so the
# registry diff must allow no extras.  P2A-MODEL's 0004 tables are modeled and
# therefore present in metadata but not in a 0003-migrated schema.
LATER_REVISION_TABLES = {
    "agent_access_grants",
    "agent_application_state_snapshots",
    "agent_approvals",
    "agent_clarification_requests",
    "agent_external_tool_bindings",
    "agent_index_outbox",
    "agent_memories",
    "agent_memory_conflicts",
    "agent_memory_consents",
    "agent_memory_extraction_outbox",
    "agent_memory_predicate_registry",
    "agent_memory_revisions",
    "agent_memory_summaries",
    "agent_memory_vector_outbox",
    "agent_messages",
    "agent_model_invocations",
    "agent_node_executions",
    "agent_ontology_bindings",
    "agent_purge_jobs",
    "agent_purge_markers",
    "agent_reconciliation_cases",
    "agent_retrieval_sources",
    "agent_runtime_events",
    "agent_sessions",
    "agent_skill_bindings",
    "agent_stream_tickets",
    "agent_tool_executions",
    "agent_turn_checkpoint_writes",
    "agent_turn_checkpoints",
    "agent_turn_dispatch_outbox",
    "agent_turns",
    "agent_versions",
    "agents",
    "application_state_schema_registries",
    "application_state_schema_versions",
    "business_journey_model_calls",
    "business_journey_preparations",
    "chatgpt_account_links",
    "chatgpt_login_transactions",
    "entity_instance_relations",
    "governed_turn_plans",
    "managed_action_bindings",
    "mcp_connection_schemas",
    "mcp_oauth_tokens",
    "mcp_write_requests",
    "model_config_versions",
    "model_credentials",
    "model_migration_findings",
    "oauth_authorization_codes",
    "oauth_clients",
    "oauth_refresh_families",
    "oauth_refresh_tokens",
    "ontology_data_grants",
    "pipeline_run_inputs",
    "prompt_generations",
    "refresh_dead_letters",
    "refresh_inbox_events",
    "refresh_outbox_events",
    "refresh_run_transitions",
    "refresh_runs",
    "refresh_schedules",
    "refresh_source_states",
    "retention_epochs",
    "retention_holds",
    "retention_policies",
    "retention_policy_versions",
    "runtime_artifacts",
    "runtime_delegated_credentials",
    "runtime_execution_approvals",
    "runtime_executions",
    "runtime_plans",
    "runtime_reconciliation_cases",
    "sandbox_simulations",
    "semantic_snapshot_inputs",
    "semantic_snapshots",
    "skill_packages",
    "skill_signatures",
    "skill_versions",
    "tool_connection_versions",
    "tool_connections",
    "tool_providers",
}


def test_p1a_integrate_red_contract():
    failures = []
    source = MIGRATION.read_text()
    for helper in (
        "upgrade_domain_foundation",
        "upgrade_release_foundation",
        "upgrade_audit_foundation",
        "upgrade_identity_foundation",
    ):
        if source.count(helper) == 0:
            failures.append(f"0003 upgrade missing {helper}")
    user_source = (BACKEND_DIR / "app" / "models" / "user.py").read_text()
    if "security_domain_id" not in user_source:
        failures.append("User mapping lacks security_domain_id")
    ontology_source = (BACKEND_DIR / "app" / "models" / "ontology.py").read_text()
    if "security_domain_id" not in ontology_source:
        failures.append("OntologyProject mapping lacks security_domain_id")
    models_init = (BACKEND_DIR / "app" / "models" / "__init__.py").read_text()
    for model_module in (
        "security_domain",
        "auth_refresh",
        "ontology_release",
        "governance_audit",
        "entity_property_definition",
    ):
        if model_module not in models_init:
            failures.append(f"load_all_models missing {model_module}")
    if failures:
        pytest.fail("RED_P1A_INTEGRATE: " + "; ".join(failures))


def _scoped_url(schema):
    return f"{TEST_DATABASE_URL}?options={quote(f'-csearch_path={schema}', safe='-=')}"


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
def full_schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p1a_integrate_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield schema
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _connection(schema):
    return create_engine(_scoped_url(schema))


def test_migration_calls_upstream_helpers_in_normative_order(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_0003_full", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "preflight_pgcrypto", lambda: None)
    for helper in (
        "upgrade_domain_foundation",
        "upgrade_release_foundation",
        "upgrade_audit_foundation",
        "upgrade_identity_foundation",
        "upgrade_access_foundation",
        "upgrade_cutover_guards",
        "upgrade_legacy_runtime_tables",
        "upgrade_working_copy_foundation",
    ):
        monkeypatch.setattr(module, helper, (lambda name: lambda: calls.append(name))(helper))
    module.upgrade()
    assert calls == [
        "upgrade_domain_foundation",
        "upgrade_release_foundation",
        "upgrade_audit_foundation",
        "upgrade_identity_foundation",
        "upgrade_access_foundation",
        "upgrade_cutover_guards",
        "upgrade_legacy_runtime_tables",
        "upgrade_working_copy_foundation",
    ]
    calls.clear()
    for helper in (
        "downgrade_working_copy_foundation",
        "downgrade_legacy_runtime_tables",
        "downgrade_cutover_guards",
        "downgrade_access_foundation",
        "downgrade_identity_foundation",
        "downgrade_audit_foundation",
        "downgrade_release_foundation",
        "downgrade_domain_foundation",
    ):
        monkeypatch.setattr(module, helper, (lambda name: lambda: calls.append(name))(helper))
    module.downgrade()
    assert calls == [
        "downgrade_working_copy_foundation",
        "downgrade_legacy_runtime_tables",
        "downgrade_cutover_guards",
        "downgrade_access_foundation",
        "downgrade_identity_foundation",
        "downgrade_audit_foundation",
        "downgrade_release_foundation",
        "downgrade_domain_foundation",
    ]


def test_fresh_0003_upgrade_schema_and_orm_registry(full_schema):
    result = _alembic(full_schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    engine = _connection(full_schema)
    inspector = inspect(engine)
    migrated_tables = set(inspector.get_table_names())
    assert NEW_0003_TABLES <= migrated_tables

    from app.database import Base
    from app.models import load_all_models

    load_all_models()
    metadata_tables = set(Base.metadata.tables)
    assert NEW_0003_TABLES <= metadata_tables  # every 0003 table has a model
    assert metadata_tables - migrated_tables == LATER_REVISION_TABLES

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003_publication_governance"
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        ontology_columns = {column["name"] for column in inspector.get_columns("ontology_projects")}
        assert "security_domain_id" in user_columns
        assert "security_domain_id" in ontology_columns
        assert "latest_published_release_id" in ontology_columns
        nullable = {column["name"] for column in inspector.get_columns("users") if not column["nullable"]}
        assert "security_domain_id" in nullable
        releases = {fk["name"]: fk for fk in inspector.get_foreign_keys("ontology_releases")}
        assert releases["fk_ontology_releases_ontology"]["options"]["ondelete"] == "RESTRICT"
        triggers = {row[0] for row in connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'ontology_releases'::regclass AND NOT tgisinternal"))}
        assert {"ontology_releases_validate_domain", "ontology_releases_immutable"} <= triggers
        audit_triggers = {row[0] for row in connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'governance_audit_logs'::regclass AND NOT tgisinternal"))}
        assert "governance_audit_logs_append_only" in audit_triggers
    engine.dispose()


def test_domain_aware_orm_roots_write_default_domain(full_schema):
    _alembic(full_schema, "upgrade", "0003_publication_governance")
    engine = _connection(full_schema)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        from app.models.user import User
        from app.models.ontology import OntologyProject

        user = User(username="domain-user", email="domain@example.com", password_hash="hash", role="editor")
        session.add(user)
        session.flush()
        assert user.security_domain_id == DEFAULT_DOMAIN
        ontology = OntologyProject(name="Domain ontology", domain="test", created_by=user.id)
        session.add(ontology)
        session.flush()
        assert ontology.security_domain_id == DEFAULT_DOMAIN
        user_id, ontology_id = user.id, ontology.id
        session.commit()
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM users WHERE id=:id AND security_domain_id=:domain"
        ), {"id": user_id, "domain": DEFAULT_DOMAIN}).scalar_one() == 1
        assert connection.execute(text(
            "SELECT count(*) FROM ontology_projects WHERE id=:id AND security_domain_id=:domain"
        ), {"id": ontology_id, "domain": DEFAULT_DOMAIN}).scalar_one() == 1
    engine.dispose()


def test_populated_0002_upgrade_backfills_domain_and_legacy_writers_stay_compatible(full_schema):
    _alembic(full_schema, "upgrade", "0002_entity_identifiers")
    engine = _connection(full_schema)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('legacy-user','legacy','legacy@example.com','hash','admin',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at) "
            "VALUES ('legacy-ontology','Legacy','test','v0.1','draft','legacy-user',now(),now())"
        ))
    engine.dispose()

    result = _alembic(full_schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    engine = _connection(full_schema)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT security_domain_id FROM users WHERE id='legacy-user'")).scalar_one() == DEFAULT_DOMAIN
        assert connection.execute(text("SELECT security_domain_id FROM ontology_projects WHERE id='legacy-ontology'")).scalar_one() == DEFAULT_DOMAIN
        # pre-Agent writers (no security_domain_id supplied) still resolve the singleton domain
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('old-writer','old','old@example.com','hash','viewer',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at) "
            "VALUES ('old-ontology','Old','test','v0.1','draft','old-writer',now(),now())"
        ))
        assert connection.execute(text(
            "SELECT count(*) FROM users WHERE security_domain_id=:domain"
        ), {"domain": DEFAULT_DOMAIN}).scalar_one() == 2
        assert connection.execute(text(
            "SELECT count(*) FROM ontology_projects WHERE security_domain_id=:domain"
        ), {"domain": DEFAULT_DOMAIN}).scalar_one() == 2
        # the full schema is usable end to end: release insert + audit chain append
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at,security_domain_id) "
            "VALUES ('creator','creator','creator@example.com','hash','admin',true,now(),now(), :domain)"
        ), {"domain": DEFAULT_DOMAIN})
        connection.execute(text(
            "INSERT INTO entities (id, ontology_id, name_cn, properties, confidence, version, created_at, updated_at) "
            "VALUES ('entity-1', 'legacy-ontology', '实体', '{}'::jsonb, 1.0, 'v0.1', now(), now())"
        ))
        release_id = "20000000-0000-0000-0000-000000000001"
        import hashlib
        manifest_bytes = b'{"manifest_version":"ontology-manifest-v1"}'
        connection.execute(text(
            "INSERT INTO ontology_releases (id, ontology_id, version_no, version, manifest_bytes, manifest_projection, schema_hash, created_by) "
            "VALUES (:id, 'legacy-ontology', 1, 'v1', :bytes, CAST(:projection AS jsonb), :hash, 'creator')"
        ), {
            "id": release_id,
            "bytes": manifest_bytes,
            "projection": manifest_bytes.decode(),
            "hash": hashlib.sha256(manifest_bytes).digest(),
        })
        connection.execute(text("UPDATE ontology_projects SET latest_published_release_id=:id WHERE id='legacy-ontology'"), {"id": release_id})
    from app.services.governance_audit import append_audit

    with engine.begin() as connection:
        receipt = append_audit(
            connection,
            security_domain_id=DEFAULT_DOMAIN,
            operation="ontology.publish",
            decision="allow",
            outcome="succeeded",
            correlation_id="integrate-corr",
            actor_user_id="creator",
            release_id=release_id,
        )
    assert receipt["sequence"] == 1
    engine.dispose()


def test_bridge_startup_imports_app_main_without_schema_repair(full_schema):
    _alembic(full_schema, "upgrade", "0003_publication_governance")
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=BACKEND_DIR,
        env=dict(os.environ, DATABASE_URL=_scoped_url(full_schema)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    engine = _connection(full_schema)
    with engine.connect() as connection:
        # importing app.main must never stamp, upgrade, or repair the database
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003_publication_governance"
        assert connection.execute(text("SELECT count(*) FROM security_domains")).scalar_one() == 1
    engine.dispose()


def test_empty_downgrade_to_0002_and_reupgrade(full_schema):
    _alembic(full_schema, "upgrade", "0003_publication_governance")
    result = _alembic(full_schema, "downgrade", "0002_entity_identifiers")
    assert result.returncode == 0, result.stderr
    engine = _connection(full_schema)
    inspector = inspect(engine)
    assert not NEW_0003_TABLES & set(inspector.get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_entity_identifiers"
        assert "security_domain_id" not in {column["name"] for column in inspector.get_columns("users")}
        assert "security_domain_id" not in {column["name"] for column in inspector.get_columns("ontology_projects")}
    engine.dispose()

    result = _alembic(full_schema, "upgrade", "0003_publication_governance")
    assert result.returncode == 0, result.stderr
    engine = _connection(full_schema)
    inspector = inspect(engine)
    assert NEW_0003_TABLES <= set(inspector.get_table_names())
    engine.dispose()
