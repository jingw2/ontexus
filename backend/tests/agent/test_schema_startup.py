import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, event, text


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
RUN_MIGRATIONS = BACKEND_DIR / "scripts" / "run_migrations.py"
VERIFY_SCHEMA = BACKEND_DIR / "scripts" / "verify_schema_revision.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _table_contract():
    return {
        "columns": ["id"],
        "primary_key": ["id"],
        "unique": [],
        "foreign_keys": [],
        "checks": [],
        "indexes": [],
        "triggers": [],
    }


def test_e0_db_red_contract():
    missing = [
        path
        for path in [
            BACKEND_DIR / "scripts" / "run_migrations.py",
            BACKEND_DIR / "scripts" / "verify_schema_revision.py",
        ]
        if not path.exists()
    ]
    if missing:
        pytest.fail(
            "RED_E0_DB: production migration/startup contract missing: "
            + ", ".join(str(path.relative_to(BACKEND_DIR)) for path in missing)
        )


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_migration_wrapper_guards_before_direct_exec(monkeypatch):
    module = _load_script("run_migrations", RUN_MIGRATIONS)
    calls = []
    monkeypatch.setattr(module, "require_supported_python", lambda: calls.append("guard"))
    monkeypatch.setattr(
        module.os,
        "execvpe",
        lambda file, argv, env: calls.append((file, argv, env)),
    )
    monkeypatch.setattr(sys, "argv", [str(RUN_MIGRATIONS), "upgrade", "head"])

    module.main()

    assert calls[0] == "guard"
    assert calls[1] == (
        sys.executable,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        os.environ,
    )


def test_schema_verifier_supports_documented_module_entrypoint():
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.verify_schema_revision", "--help"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--build-manifest" in proc.stdout


def test_migration_wrapper_rejects_real_python_3_10_before_alembic_import():
    python = pathlib.Path.home() / ".local/share/uv/python/cpython-3.10.20-macos-aarch64-none/bin/python3.10"
    if not python.exists():
        pytest.skip("real Python 3.10 runtime unavailable")
    proc = subprocess.run(
        [str(python), str(RUN_MIGRATIONS), "--help"], capture_output=True, text=True
    )
    assert proc.returncode != 0
    assert "UNSUPPORTED_PYTHON_VERSION" in proc.stderr + proc.stdout
    # the guard must fire before Alembic starts: its CLI usage text must be absent
    assert "Usage: alembic" not in proc.stderr + proc.stdout


def test_alembic_env_guards_before_third_party_imports():
    tree = ast.parse((BACKEND_DIR / "alembic" / "env.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    guard_line = min(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "require_supported_python"
    )
    third_party_lines = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
            if root in {"alembic", "sqlalchemy"}:
                third_party_lines.append(node.lineno)
    assert third_party_lines and guard_line < min(third_party_lines)


def test_load_all_models_registers_current_milestone_without_database_io():
    from app.database import Base
    from app.models import load_all_models

    before = set(Base.metadata.tables)
    assert load_all_models() is Base.metadata
    after = set(Base.metadata.tables)
    assert before <= after
    assert after == {
        "actions",
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
        "audit_tasks",
        "auth_refresh_families",
        "auth_refresh_tokens",
        "business_journey_model_calls",
        "business_journey_preparations",
        "chatgpt_account_links",
        "chatgpt_login_transactions",
        "entities",
        "entity_instance_relations",
        "entity_instances",
        "entity_property_definitions",
        "extraction_tasks",
        "governance_audit_chain_heads",
        "governance_audit_logs",
        "governance_audit_outbox",
        "governed_turn_plans",
        "logic_rules",
        "managed_action_bindings",
        "mcp_connection_schemas",
        "mcp_oauth_tokens",
        "mcp_write_requests",
        "model_config_versions",
        "model_configs",
        "model_credentials",
        "model_migration_findings",
        "oauth_authorization_codes",
        "oauth_clients",
        "oauth_refresh_families",
        "oauth_refresh_tokens",
        "ontology_data_grants",
        "ontology_migration_findings",
        "ontology_project_access_grants",
        "ontology_projects",
        "ontology_releases",
        "pipeline_run_inputs",
        "prompt_generations",
        "prompts",
        "refresh_dead_letters",
        "refresh_inbox_events",
        "refresh_outbox_events",
        "refresh_run_transitions",
        "refresh_runs",
        "refresh_schedules",
        "refresh_source_states",
        "relations",
        "retention_epochs",
        "retention_holds",
        "retention_policies",
        "retention_policy_versions",
        "rules_config",
        "runtime_artifacts",
        "runtime_delegated_credentials",
        "runtime_execution_approvals",
        "runtime_executions",
        "runtime_plans",
        "runtime_reconciliation_cases",
        "sandbox_simulations",
        "security_domains",
        "semantic_snapshot_inputs",
        "semantic_snapshots",
        "skill_packages",
        "skill_signatures",
        "skill_versions",
        "tool_connection_versions",
        "tool_connections",
        "tool_providers",
        "uploaded_files",
        "users",
        "v2_connections",
        "v2_curated_datasets",
        "v2_curated_reviews",
        "v2_curated_row_edits",
        "v2_dataset_versions",
        "v2_datasets",
        "v2_media_items",
        "v2_ontology_action_runs",
        "v2_ontology_action_types",
        "v2_ontology_link_mappings",
        "v2_ontology_logic_rules",
        "v2_ontology_mappings",
        "v2_ontology_state_machines",
        "v2_pipeline_runs",
        "v2_pipeline_versions",
        "v2_pipelines",
    } <= after


def test_application_startup_contains_no_schema_repair():
    source = (BACKEND_DIR / "app" / "main.py").read_text()
    assert "_run_schema_migration" not in source
    assert "command.stamp" not in source
    assert "command.upgrade" not in source
    assert "metadata.create_all" not in source


def test_readmes_use_only_guarded_install_and_migration_commands():
    for readme in [BACKEND_DIR.parent / "README.md", BACKEND_DIR.parent / "README_zh.md"]:
        source = readme.read_text()
        assert "python scripts/bootstrap_backend.py" in source
        assert "python scripts/run_migrations.py upgrade head" in source
        assert "pip install -r requirements.txt" not in source
        assert "alembic upgrade head" not in source


def test_manifest_parser_accepts_exact_or_closed_compatible_set(tmp_path):
    module = _load_script("verify_schema_revision", VERIFY_SCHEMA)
    exact = tmp_path / "exact.json"
    exact.write_text(json.dumps({"schema_contract_version": 1, "schema_revision": "rev-a", "critical_schema": {"tables": {"critical": _table_contract()}}}))
    compatible = tmp_path / "compatible.json"
    compatible.write_text(json.dumps({"schema_contract_version": 1, "compatible_schema_revisions": ["rev-a", "rev-b"], "critical_schema": {"tables": {"critical": _table_contract()}}}))

    assert module.load_manifest(exact).accepted_revisions == frozenset({"rev-a"})
    assert module.load_manifest(compatible).accepted_revisions == frozenset({"rev-a", "rev-b"})

    for payload in [
        {},
        {"schema_contract_version": 1, "schema_revision": "head", "critical_schema": {"tables": {}}},
        {"schema_contract_version": 1, "schema_revision": "rev-a", "compatible_schema_revisions": ["rev-a"], "critical_schema": {"tables": {}}},
        {"schema_contract_version": 1, "compatible_schema_revisions": ["rev-a", "rev-a"], "critical_schema": {"tables": {}}},
    ]:
        path = tmp_path / f"invalid-{uuid.uuid4()}.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(module.ManifestError, match="BUILD_MANIFEST_INVALID"):
            module.load_manifest(path)


def test_manifest_parser_rejects_vacuous_and_malformed_contracts(tmp_path):
    module = _load_script("verify_schema_revision_contracts", VERIFY_SCHEMA)
    with pytest.raises(module.ManifestError, match="BUILD_MANIFEST_INVALID"):
        module.load_manifest(tmp_path / "missing.json")
    invalid_contracts = [
        {},
        {"columns": []},
        {**_table_contract(), "columns": ["id", "id"]},
        {**_table_contract(), "foreign_keys": [{"columns": ["parent_id"]}]},
        {**_table_contract(), "checks": [{"name": "positive", "sql": ""}]},
        {**_table_contract(), "indexes": [{"name": "lookup", "unique": False, "expressions": []}]},
        {**_table_contract(), "triggers": [{"name": "audit", "definition": "", "enabled": "O"}]},
    ]
    payloads = [
        {"schema_contract_version": 1, "schema_revision": "rev-a", "critical_schema": {"tables": {}}},
        *[
            {"schema_contract_version": 1, "schema_revision": "rev-a", "critical_schema": {"tables": {"critical": contract}}}
            for contract in invalid_contracts
        ],
    ]

    for payload in payloads:
        path = tmp_path / f"invalid-contract-{uuid.uuid4()}.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(module.ManifestError, match="BUILD_MANIFEST_INVALID"):
            module.load_manifest(path)


def test_manifest_parser_accepts_only_bounded_production_envelope(tmp_path):
    module = _load_script("verify_schema_revision_envelope", VERIFY_SCHEMA)
    envelope = {
        "manifest_version": 1,
        "image_digest": "sha256:image",
        "source_digest": "sha256:source",
        "runtime_artifact_tuple": ["langgraph", "graph-v1", "serializer-v1"],
        "python_lock_hash": "sha256:python",
        "dependency_lock_hash": "sha256:dependencies",
        "signer_identity": "release@example.test",
        "signature": "base64-signature-owned-by-e0-images",
        "schema_contract": {
            "schema_contract_version": 1,
            "schema_revision": "rev-a",
            "critical_schema": {"tables": {"critical": _table_contract()}},
        },
    }
    path = tmp_path / "production-manifest.json"
    path.write_text(json.dumps(envelope))

    manifest = module.load_manifest(path)

    assert manifest.accepted_revisions == frozenset({"rev-a"})
    assert tuple(manifest.critical_tables) == ("critical",)
    with pytest.raises(TypeError):
        manifest.critical_tables["other"] = manifest.critical_tables["critical"]

    for mutation in [
        lambda value: value.update({"schema_revision": "environment-override"}),
        lambda value: value.update({"unknown_metadata": "not-allowed"}),
        lambda value: value.pop("signature"),
    ]:
        invalid = json.loads(json.dumps(envelope))
        mutation(invalid)
        path.write_text(json.dumps(invalid))
        with pytest.raises(module.ManifestError, match="BUILD_MANIFEST_INVALID"):
            module.load_manifest(path)


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL required")
def test_postgresql_verifier_is_fail_closed_and_read_only(tmp_path):
    module = _load_script("verify_schema_revision_pg", VERIFY_SCHEMA)
    schema = "e0_db_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_contract_version": 1,
        "schema_revision": "0002_entity_identifiers",
        "critical_schema": {"tables": {
            "parents": {
                "columns": ["tenant_id", "id"],
                "primary_key": ["tenant_id", "id"],
                "unique": [],
                "foreign_keys": [],
                "checks": [],
                "indexes": [],
                "triggers": [],
            },
            "children": {
                "columns": ["tenant_id", "id", "parent_id", "code", "score"],
                "primary_key": ["tenant_id", "id"],
                "unique": [["tenant_id", "code"]],
                "foreign_keys": [{
                    "columns": ["tenant_id", "parent_id"],
                    "referred_schema": None,
                    "referred_table": "parents",
                    "referred_columns": ["tenant_id", "id"],
                    "ondelete": "CASCADE",
                }],
                "checks": [{"name": "children_score_check", "sql": "score > 0"}],
                "indexes": [{
                    "name": "children_lower_code_idx",
                    "unique": True,
                    "expressions": ["tenant_id", "lower(code)"],
                }],
                "triggers": [{
                    "name": "children_guard",
                    "definition": (
                        "CREATE TRIGGER children_guard BEFORE INSERT OR UPDATE ON children "
                        "FOR EACH ROW EXECUTE FUNCTION child_noop()"
                    ),
                    "enabled": "O",
                }],
            },
        }},
    }))
    manifest = module.load_manifest(manifest_path)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))

            with pytest.raises(module.SchemaVerificationError, match="DATABASE_REVISION_MISMATCH"):
                module.verify_connection(connection, manifest)

            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('wrong')"))
            connection.execute(text(
                "CREATE TABLE parents (tenant_id TEXT NOT NULL, id TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, id))"
            ))
            connection.execute(text(
                "CREATE TABLE children ("
                "tenant_id TEXT NOT NULL, id TEXT NOT NULL, parent_id TEXT NOT NULL, "
                "code TEXT NOT NULL, score INTEGER NOT NULL, "
                "PRIMARY KEY (tenant_id, id), "
                "CONSTRAINT children_code_key UNIQUE (tenant_id, code), "
                "CONSTRAINT children_parent_fk FOREIGN KEY (tenant_id, parent_id) "
                "REFERENCES parents (tenant_id, id) ON DELETE CASCADE, "
                "CONSTRAINT children_score_check CHECK (score > 0))"
            ))
            connection.execute(text(
                "CREATE UNIQUE INDEX children_lower_code_idx "
                "ON children (tenant_id, lower(code))"
            ))
            connection.execute(text(
                "CREATE FUNCTION child_noop() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RETURN NEW; END $$"
            ))
            connection.execute(text(
                "CREATE TRIGGER children_guard BEFORE INSERT OR UPDATE ON children "
                "FOR EACH ROW EXECUTE FUNCTION child_noop()"
            ))
            with pytest.raises(module.SchemaVerificationError, match="DATABASE_REVISION_MISMATCH"):
                module.verify_connection(connection, manifest)

            connection.execute(text("UPDATE alembic_version SET version_num='0002_entity_identifiers'"))
            module.verify_connection(connection, manifest)

        mutations = [
            "ALTER TABLE children RENAME TO changed_children",
            "ALTER TABLE children RENAME COLUMN score TO changed_score",
            (
                "ALTER TABLE children DROP CONSTRAINT children_pkey; "
                "ALTER TABLE children ADD PRIMARY KEY (id, tenant_id)"
            ),
            (
                "ALTER TABLE children DROP CONSTRAINT children_code_key; "
                "ALTER TABLE children ADD UNIQUE (code, tenant_id)"
            ),
            (
                "ALTER TABLE children DROP CONSTRAINT children_parent_fk; "
                "ALTER TABLE children ADD FOREIGN KEY (parent_id, tenant_id) "
                "REFERENCES parents (id, tenant_id) ON DELETE CASCADE"
            ),
            (
                "ALTER TABLE children DROP CONSTRAINT children_parent_fk; "
                "ALTER TABLE children ADD FOREIGN KEY (tenant_id, parent_id) "
                "REFERENCES parents (tenant_id, id) ON DELETE RESTRICT"
            ),
            (
                "ALTER TABLE children DROP CONSTRAINT children_score_check; "
                "ALTER TABLE children ADD CONSTRAINT children_score_check CHECK (score >= 0)"
            ),
            "ALTER TABLE children RENAME CONSTRAINT children_score_check TO changed_score_check",
            (
                "DROP INDEX children_lower_code_idx; "
                "CREATE INDEX children_lower_code_idx ON children (tenant_id, lower(code))"
            ),
            (
                "DROP INDEX children_lower_code_idx; "
                "CREATE UNIQUE INDEX children_lower_code_idx ON children (tenant_id, upper(code))"
            ),
            "ALTER INDEX children_lower_code_idx RENAME TO changed_lower_code_idx",
            "ALTER TABLE children DISABLE TRIGGER children_guard",
            (
                "DROP TRIGGER children_guard ON children; "
                "CREATE TRIGGER children_guard BEFORE INSERT ON children "
                "FOR EACH ROW EXECUTE FUNCTION child_noop()"
            ),
            "ALTER TRIGGER children_guard ON children RENAME TO changed_guard",
        ]
        for mutation in mutations:
            with engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(text(f'SET search_path TO "{schema}"'))
                for statement in mutation.split("; "):
                    connection.execute(text(statement))
                with pytest.raises(module.SchemaVerificationError, match="DATABASE_SCHEMA_DRIFT"):
                    module.verify_connection(connection, manifest)
                transaction.rollback()

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.execute(text("INSERT INTO alembic_version VALUES ('second-head')"))
            with pytest.raises(module.SchemaVerificationError, match="DATABASE_REVISION_MISMATCH"):
                module.verify_connection(connection, manifest)
            transaction.rollback()

        statements = []
        listener = lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement)
        event.listen(engine, "before_cursor_execute", listener)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.execute(text("SET TRANSACTION READ ONLY"))
                statements.clear()
                module.verify_connection(connection, manifest)
                transaction.rollback()
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        assert statements
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
