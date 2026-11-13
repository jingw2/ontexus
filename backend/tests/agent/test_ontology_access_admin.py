"""P1A-ACCESS: governed ontology project access.

Covers the `ontology_project_access_grants` table + creator-grant backfill
(0003 access-foundation helper), closed capability vocabulary with exact role
ceilings, monotonic-revision CAS create/revise/revoke with no self-escalation,
the unresolved-owner recovery path, and typed property migration-remediation
routes that CAS on `working_revision`, resolve findings, and audit.

PostgreSQL-marked tests use TEST_DATABASE_URL; SQLite never substitutes.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = Path(__file__).resolve().parents[2]
MODEL = BACKEND_DIR / "app" / "models" / "ontology_access.py"
SERVICE = BACKEND_DIR / "app" / "services" / "ontology_access.py"
REMEDIATION = BACKEND_DIR / "app" / "services" / "publication" / "remediation.py"
GRANTS_ROUTER = BACKEND_DIR / "app" / "routers" / "ontology_access_grants.py"
REMEDIATIONS_ROUTER = BACKEND_DIR / "app" / "routers" / "ontology_remediations.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DEFAULT_DOMAIN = "00000000-0000-0000-0000-000000000001"


def test_p1a_access_red_contract():
    missing = [path for path in (MODEL, SERVICE, REMEDIATION, GRANTS_ROUTER, REMEDIATIONS_ROUTER) if not path.exists()]
    if missing:
        pytest.fail(
            "RED_P1A_ACCESS: ontology project access foundation missing: "
            + ", ".join(str(path.relative_to(BACKEND_DIR)) for path in missing)
        )
    migration_source = (BACKEND_DIR / "alembic" / "versions" / "0003_publication_governance.py").read_text()
    for marker in ("upgrade_access_foundation", "downgrade_access_foundation"):
        if marker not in migration_source:
            pytest.fail(f"RED_P1A_ACCESS: 0003 missing {marker}")


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
def access_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p1a_access_" + uuid.uuid4().hex
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


def _insert_user(Session, username, role="editor", active=True, domain=True, domain_id=None):
    domain_clause = ", security_domain_id" if domain else ""
    domain_value = ", :domain" if domain else ""
    params = {
        "id": str(uuid.uuid4()),
        "username": username,
        "email": f"{username}@example.com",
        "role": role,
        "active": active,
    }
    if domain:
        params["domain"] = domain_id or DEFAULT_DOMAIN
    with Session() as session:
        session.execute(text(
            f"INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at{domain_clause}) "
            f"VALUES (:id, :username, :email, 'hash', :role, :active, now(), now(){domain_value})"
        ), params)
        user_id = session.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar_one()
        session.commit()
        return user_id


def _insert_ontology(Session, name, created_by, domain_id=None):
    with Session() as session:
        session.execute(text(
            "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at, security_domain_id) "
            "VALUES (:id, :name, 'test', 'v0.1', 'draft', :created_by, now(), now(), :domain)"
        ), {"id": str(uuid.uuid4()), "name": name, "created_by": created_by, "domain": domain_id or DEFAULT_DOMAIN})
        ontology_id = session.execute(text("SELECT id FROM ontology_projects WHERE name=:n"), {"n": name}).scalar_one()
        session.commit()
        return ontology_id


def _current_grant_row(Session, ontology_id, user_id):
    with Session() as session:
        row = session.execute(text(
            "SELECT capabilities, revision, status FROM ontology_project_access_grants "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {"o": ontology_id, "u": user_id}).mappings().one_or_none()
        return row


# ── unit: capability vocabulary and role ceilings (DB-free) ──────────────────

def test_capability_vocabulary_is_closed_and_role_ceilings_are_exact():
    from app.services.ontology_access import CAPABILITIES, ROLE_CEILINGS, validate_capabilities

    assert CAPABILITIES == ("discover", "read", "edit", "publish")
    assert ROLE_CEILINGS["viewer"] == {"discover", "read"}
    assert ROLE_CEILINGS["editor"] == set(CAPABILITIES)
    assert ROLE_CEILINGS["admin"] == set(CAPABILITIES)
    validate_capabilities(["discover", "read"])
    with pytest.raises(ValueError):
        validate_capabilities(["discover", "admin"])
    with pytest.raises(ValueError):
        validate_capabilities([])


# ── PostgreSQL: backfill, CAS grant ledger, recovery, remediation ────────────

def test_zz_backfill_assigns_creator_grants_and_recovery_findings():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    from app.services.ontology_access import CAPABILITIES

    schema = "p1a_access_backfill_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        result = _alembic(schema, "upgrade", "0002_entity_identifiers")
        assert result.returncode == 0, result.stderr
        Session = sessionmaker(bind=create_engine(_scoped_url(schema)))
        with Session() as session:
            admin = _insert_user(Session, "backfill-admin", role="admin", domain=False)
            editor = _insert_user(Session, "backfill-editor", role="editor", domain=False)
            viewer = _insert_user(Session, "backfill-viewer", role="viewer", domain=False)
            inactive = _insert_user(Session, "backfill-inactive", role="admin", active=False, domain=False)
            session.execute(text(
                "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at) "
                "VALUES (:id, 'Admin ontology', 'test', 'v0.1', 'draft', :creator, now(), now())"
            ), {"id": str(uuid.uuid4()), "creator": admin})
            session.execute(text(
                "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at) "
                "VALUES (:id, 'Editor ontology', 'test', 'v0.1', 'draft', :creator, now(), now())"
            ), {"id": str(uuid.uuid4()), "creator": editor})
            session.execute(text(
                "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at) "
                "VALUES (:id, 'Viewer ontology', 'test', 'v0.1', 'draft', :creator, now(), now())"
            ), {"id": str(uuid.uuid4()), "creator": viewer})
            session.execute(text(
                "INSERT INTO ontology_projects (id, name, domain, version, status, created_by, created_at, updated_at) "
                "VALUES (:id, 'Inactive ontology', 'test', 'v0.1', 'draft', :creator, now(), now())"
            ), {"id": str(uuid.uuid4()), "creator": inactive})
            session.commit()
        _alembic(schema, "upgrade", "0003_publication_governance")
        with Session() as session:
            rows = session.execute(text(
                "SELECT o.name, g.capabilities::text AS capabilities, g.status FROM ontology_project_access_grants g "
                "JOIN ontology_projects o ON o.id = g.ontology_id ORDER BY o.name"
            )).mappings().all()
            by_name = {row["name"]: row for row in rows}
            assert set(json.loads(by_name["Admin ontology"]["capabilities"])) == set(CAPABILITIES)
            assert set(json.loads(by_name["Editor ontology"]["capabilities"])) == set(CAPABILITIES)
            assert set(json.loads(by_name["Viewer ontology"]["capabilities"])) == {"discover", "read"}
            assert "Inactive ontology" not in by_name
            findings = session.execute(text(
                "SELECT ontology_id, kind, code, status FROM ontology_migration_findings WHERE kind='owner' ORDER BY ontology_id"
            )).mappings().all()
            finding_ontologies = {row["ontology_id"] for row in findings}
            viewer_ontology = session.execute(text("SELECT id FROM ontology_projects WHERE name='Viewer ontology'")).scalar_one()
            inactive_ontology = session.execute(text("SELECT id FROM ontology_projects WHERE name='Inactive ontology'")).scalar_one()
            assert {viewer_ontology, inactive_ontology} == finding_ontologies
            assert all(row["code"] == "ONTOLOGY_OWNER_RECOVERY_REQUIRED" for row in findings)
            assert all(row["status"] == "open" for row in findings)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_zz_backfill_creator_grants_repairs_bypassed_rows(access_db):
    """Issue-1 repair path: ontologies written without the creator grant get one."""
    from app.services.ontology_access import CAPABILITIES, backfill_creator_grants

    Session, _ = access_db
    admin = _insert_user(Session, "repair-admin", role="admin")
    editor = _insert_user(Session, "repair-editor", role="editor")
    viewer = _insert_user(Session, "repair-viewer", role="viewer")
    inactive = _insert_user(Session, "repair-inactive", role="admin", active=False)
    admin_onto = _insert_ontology(Session, "Repair admin ontology", admin)
    editor_onto = _insert_ontology(Session, "Repair editor ontology", editor)
    viewer_onto = _insert_ontology(Session, "Repair viewer ontology", viewer)
    inactive_onto = _insert_ontology(Session, "Repair inactive ontology", inactive)

    with Session() as session:
        # no grants exist yet (direct-SQL rows bypass the creator-grant path)
        assert session.execute(text(
            "SELECT count(*) FROM ontology_project_access_grants"
        )).scalar_one() == 0
        report = backfill_creator_grants(session)
        assert report["editor_or_admin_grants"] == 2
        assert report["viewer_grants"] == 1
        assert report["grant_rows_inserted"] == 3
        # viewer and inactive creators both need owner recovery (mirrors 0003)
        assert report["owner_recovery_findings_inserted"] == 2

    with Session() as session:
        admin_grant = _current_grant_row(Session, admin_onto, admin)
        assert admin_grant is not None and admin_grant["status"] == "active"
        assert set(admin_grant["capabilities"]) == set(CAPABILITIES)
        editor_grant = _current_grant_row(Session, editor_onto, editor)
        assert set(editor_grant["capabilities"]) == set(CAPABILITIES)
        viewer_grant = _current_grant_row(Session, viewer_onto, viewer)
        assert set(viewer_grant["capabilities"]) == {"discover", "read"}
        assert _current_grant_row(Session, inactive_onto, inactive) is None
        findings = session.execute(text(
            "SELECT ontology_id, code, status FROM ontology_migration_findings "
            "WHERE kind='owner'"
        )).mappings().all()
        assert {row["ontology_id"] for row in findings} == {viewer_onto, inactive_onto}
        assert all(row["code"] == "ONTOLOGY_OWNER_RECOVERY_REQUIRED" for row in findings)
        assert all(row["status"] == "open" for row in findings)

    # idempotent: a second run inserts nothing
    with Session() as session:
        report = backfill_creator_grants(session)
        assert report["grant_rows_inserted"] == 0
        assert report["owner_recovery_findings_inserted"] == 0


def test_zz_grant_cas_revision_ledger_no_self_escalation_and_revoke(access_db):
    from app.models.user import User
    from app.services.ontology_access import (
        GrantConflict,
        GrantDenied,
        creator_grant,
        create_grant,
        require_project_grant,
        revise_grant,
        revoke_grant,
    )

    Session, _ = access_db
    owner = _insert_user(Session, "grant-owner", role="admin")
    editor = _insert_user(Session, "grant-editor", role="editor")
    viewer = _insert_user(Session, "grant-viewer", role="viewer")
    ontology_id = _insert_ontology(Session, "Grant ontology", owner)

    with Session() as session:
        owner_user = session.get(User, owner)
        creator_grant(session, ontology_id, owner_user)  # bootstrap path (P1B wires this into create_ontology)
        created = create_grant(
            session, ontology_id=ontology_id, user_id=editor, capabilities=["discover", "read"],
            base_revision=0, actor_id=owner,
        )
        assert created["revision"] == 1
        with pytest.raises(GrantConflict):
            create_grant(
                session, ontology_id=ontology_id, user_id=editor, capabilities=["discover"],
                base_revision=0, actor_id=owner,
            )
        # an actor may never create or revise a grant for themselves (no self-escalation)
        with pytest.raises(GrantDenied):
            create_grant(
                session, ontology_id=ontology_id, user_id=owner, capabilities=["discover"],
                base_revision=0, actor_id=owner,
            )
        # capabilities never exceed the recipient's role ceiling
        with pytest.raises(GrantDenied):
            create_grant(
                session, ontology_id=ontology_id, user_id=viewer,
                capabilities=["discover", "read", "edit"], base_revision=0, actor_id=owner,
            )
        revised = revise_grant(
            session, grant_id=created["id"], capabilities=["discover", "read", "edit"],
            base_revision=1, actor_id=owner,
        )
        assert revised["revision"] == 2
        with pytest.raises(GrantConflict):
            revise_grant(
                session, grant_id=created["id"], capabilities=["discover"],
                base_revision=1, actor_id=owner,  # stale base revision
            )
        revoked = revoke_grant(session, grant_id=created["id"], base_revision=2, actor_id=owner)
        assert revoked["status"] == "revoked" and revoked["revision"] == 3
        editor_user = session.get(User, editor)
        with pytest.raises(GrantDenied):
            require_project_grant(session, editor_user, ontology_id, "read")

    row = _current_grant_row(Session, ontology_id, editor)
    assert row["revision"] == 3 and row["status"] == "revoked"
    with Session() as session:
        outbox = session.execute(text(
            "SELECT count(*) FROM governance_audit_outbox WHERE correlation_id LIKE 'grant:%'"
        )).scalar_one()
        assert outbox >= 3


def test_zz_recovery_assigns_owner_for_unresolved_finding(access_db):
    from app.services.ontology_access import recover_owner

    Session, _ = access_db
    admin = _insert_user(Session, "recovery-admin", role="admin")
    viewer_creator = _insert_user(Session, "recovery-viewer", role="viewer")
    assignee = _insert_user(Session, "recovery-assignee", role="editor")
    ontology_id = _insert_ontology(Session, "Recovery ontology", viewer_creator)
    with Session() as session:
        session.execute(text(
            "INSERT INTO ontology_migration_findings (id, ontology_id, entity_id, kind, item_id, code, path, message, source_hash, classification, status) "
            "VALUES (:id, :o, NULL, 'owner', :o, 'ONTOLOGY_OWNER_RECOVERY_REQUIRED', 'ontologies/' || :o, 'creator not an active editor/admin', :hash, NULL, 'open')"
        ), {"id": str(uuid.uuid4()), "o": ontology_id, "hash": b"a" * 32})
        session.commit()
        base = session.execute(text(
            "SELECT revision FROM ontology_migration_findings WHERE ontology_id=:o AND kind='owner'"
        ), {"o": ontology_id}).scalar_one()
        granted = recover_owner(
            session, ontology_id=ontology_id, base_finding_revision=base,
            assignee_user_id=assignee, actor_id=admin,
        )
        assert set(granted["capabilities"]) == {"discover", "read", "edit", "publish"}
        assert granted["user_id"] == assignee
        assert session.execute(text(
            "SELECT status FROM ontology_migration_findings WHERE ontology_id=:o AND kind='owner'"
        ), {"o": ontology_id}).scalar_one() == "resolved"


def test_zz_remediation_property_cas_resolves_finding_and_increments_revision(access_db):
    from app.services.publication.preflight import normalize_property_key, stable_property_definition_id
    from app.services.publication.remediation import RemediationConflict, remediate_property

    Session, _ = access_db
    owner = _insert_user(Session, "remed-owner", role="admin")
    ontology_id = _insert_ontology(Session, "Remediation ontology", owner)
    with Session() as session:
        session.execute(text(
            "INSERT INTO entities (id, ontology_id, name_cn, properties, confidence, version, created_at, updated_at) "
            "VALUES (:id, :o, '实体', '{}'::jsonb, 1.0, 'v0.1', now(), now())"
        ), {"id": str(uuid.uuid4()), "o": ontology_id})
        entity_id = session.execute(text("SELECT id FROM entities WHERE ontology_id=:o"), {"o": ontology_id}).scalar_one()
        payload = {"example": "a value"}
        source_hash = hashlib.sha256(str(payload).encode()).digest()
        session.execute(text(
            "INSERT INTO ontology_migration_findings (id, ontology_id, entity_id, kind, item_id, code, path, message, source_hash, classification, status) "
            "VALUES (:id, :o, :e, 'property', 'Amount', 'PROPERTY_EXAMPLE_OR_SCALAR', 'entities/' || :e || '/properties/Amount', 'scalar value, never a schema', :hash, 'example_or_scalar', 'open')"
        ), {"id": str(uuid.uuid4()), "o": ontology_id, "e": entity_id, "hash": source_hash})
        session.commit()
        base_working = session.execute(text(
            "SELECT working_revision FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).scalar_one()

        from app.schemas.ontology_remediation import RemediatePropertyRequest

        # stale working revision CAS fails closed before any mutation
        with pytest.raises(RemediationConflict):
            remediate_property(
                session, ontology_id=ontology_id,
                request=RemediatePropertyRequest(
                    base_working_revision=base_working - 1, source_hash=source_hash.hex(),
                    property_key="Amount",
                    explicit_schema_metadata={"type": "number"},
                ),
                actor_id=owner,
            )
        assert session.execute(text(
            "SELECT working_revision FROM ontology_projects WHERE id=:o"
        ), {"o": ontology_id}).scalar_one() == base_working

        result = remediate_property(
            session, ontology_id=ontology_id,
            request=RemediatePropertyRequest(
                base_working_revision=base_working, source_hash=source_hash.hex(),
                property_key="Amount",
                explicit_schema_metadata={"type": "number", "required": True, "sensitivity": "internal"},
            ),
            actor_id=owner,
        )
        assert result["finding"]["status"] == "resolved"
        assert result["working_revision"] == base_working + 1
        session.commit()

        definition_id = stable_property_definition_id(ontology_id, entity_id, "Amount")
        row = session.execute(text(
            "SELECT id, normalized_key, value_type, required FROM entity_property_definitions WHERE ontology_id=:o AND entity_id=:e"
        ), {"o": ontology_id, "e": entity_id}).mappings().one()
        assert row["normalized_key"] == normalize_property_key("Amount")
        assert row["value_type"] == "number" and row["required"] is True
        assert row["id"] == definition_id

        # a resolved finding cannot be remediated again
        with pytest.raises(Exception):
            remediate_property(
                session, ontology_id=ontology_id,
                request=RemediatePropertyRequest(
                    base_working_revision=base_working + 1, source_hash=source_hash.hex(),
                    property_key="Amount",
                    explicit_schema_metadata={"type": "number"},
                ),
                actor_id=owner,
            )
        # the wrong source hash is rejected without touching the definition
        with pytest.raises(Exception):
            remediate_property(
                session, ontology_id=ontology_id,
                request=RemediatePropertyRequest(
                    base_working_revision=base_working + 1, source_hash="ab" * 32,
                    property_key="Amount",
                    explicit_schema_metadata={"type": "number"},
                ),
                actor_id=owner,
            )


def test_zz_router_grant_and_remediation_methods(access_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.deps import get_db
    from app.routers import ontology_access_grants, ontology_remediations
    from app.services.auth_service import create_access_token

    Session, _ = access_db
    owner = _insert_user(Session, "route-owner", role="admin")
    other = _insert_user(Session, "route-other", role="editor")
    ontology_id = _insert_ontology(Session, "Route ontology", owner)
    with Session() as session:
        from app.models.user import User
        from app.services.ontology_access import creator_grant

        owner_user = session.get(User, owner)
        creator_grant(session, ontology_id, owner_user)
        owner_id = session.execute(text("SELECT id FROM users WHERE username='route-owner'")).scalar_one()
        other_id = session.execute(text("SELECT id FROM users WHERE username='route-other'")).scalar_one()
    owner_token = create_access_token({"sub": owner_id, "role": "admin"})
    other_token = create_access_token({"sub": other_id, "role": "editor"})

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    test_app = FastAPI()
    test_app.include_router(ontology_access_grants.router, prefix="/api/v1/ontologies")
    test_app.include_router(ontology_access_grants.admin_router, prefix="/api/v1")
    test_app.include_router(ontology_remediations.router, prefix="/api/v1/ontologies")
    test_app.dependency_overrides[get_db] = override_get_db
    with TestClient(test_app) as client:
        # no bearer -> 403; the owner (with edit) can list grants
        assert client.get(f"/api/v1/ontologies/{ontology_id}/access-grants").status_code == 403
        listing = client.get(
            f"/api/v1/ontologies/{ontology_id}/access-grants",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert listing.status_code == 200
        assert listing.json()["data"][0]["user_id"] == owner_id
        # an actor without any grant gets an existence-hiding 404
        denied = client.get(
            f"/api/v1/ontologies/{ontology_id}/access-grants",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert denied.status_code == 404
        # recovery requires admin
        recovery = client.post(
            f"/api/v1/admin/ontology-owner-recoveries/{ontology_id}/assign",
            json={"base_finding_revision": 1, "assignee_user_id": other_id},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert recovery.status_code == 403
        # remediation page requires read; missing finding detail is existence-hidden 404
        assert client.get(f"/api/v1/ontologies/{ontology_id}/migration-remediations").status_code == 403
        page = client.get(
            f"/api/v1/ontologies/{ontology_id}/migration-remediations",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert page.status_code == 200
        missing = client.get(
            f"/api/v1/ontologies/{ontology_id}/migration-remediations/property/does-not-exist",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert missing.status_code == 404
    test_app.dependency_overrides.clear()


def test_zz_require_project_grant_enforces_actor_state_domain_window_and_role(access_db):
    from types import SimpleNamespace

    from app.models.user import User
    from app.services.ontology_access import (
        GrantDenied,
        create_grant,
        creator_grant,
        require_project_grant,
    )

    Session, _ = access_db
    owner = _insert_user(Session, "guard-owner", role="admin")
    reader = _insert_user(Session, "guard-reader", role="editor")
    target = _insert_user(Session, "guard-target", role="viewer")
    ontology_id = _insert_ontology(Session, "Guard ontology", owner)

    with Session() as session:
        creator_grant(session, ontology_id, session.get(User, owner))
        create_grant(
            session,
            ontology_id=ontology_id,
            user_id=reader,
            capabilities=["discover", "read", "edit", "publish"],
            base_revision=0,
            actor_id=owner,
        )
        reader_user = session.get(User, reader)
        assert require_project_grant(session, reader_user, ontology_id, "edit")

        reader_user.role = "viewer"
        session.flush()
        with pytest.raises(GrantDenied):
            require_project_grant(session, reader_user, ontology_id, "edit")
        assert require_project_grant(session, reader_user, ontology_id, "read")

        reader_user.role = "editor"
        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_from=:valid_from "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {
            "valid_from": datetime.now(timezone.utc) + timedelta(minutes=5),
            "o": ontology_id,
            "u": reader,
        })
        with pytest.raises(GrantDenied):
            require_project_grant(session, reader_user, ontology_id, "read")

        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_from=NULL, valid_until=:valid_until "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {
            "valid_until": datetime.now(timezone.utc) - timedelta(seconds=1),
            "o": ontology_id,
            "u": reader,
        })
        with pytest.raises(GrantDenied):
            require_project_grant(session, reader_user, ontology_id, "read")

        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_until=NULL WHERE ontology_id=:o AND user_id=:u"
        ), {"o": ontology_id, "u": reader})
        with pytest.raises(GrantDenied):
            require_project_grant(
                session,
                SimpleNamespace(
                    id=reader,
                    is_active=True,
                    role="editor",
                    security_domain_id="00000000-0000-0000-0000-000000000099",
                ),
                ontology_id,
                "read",
            )

        reader_user.is_active = False
        session.flush()
        with pytest.raises(GrantDenied):
            require_project_grant(session, reader_user, ontology_id, "read")

        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_until=:valid_until "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {
            "valid_until": datetime.now(timezone.utc) - timedelta(seconds=1),
            "o": ontology_id,
            "u": owner,
        })
        with pytest.raises(GrantDenied):
            create_grant(
                session,
                ontology_id=ontology_id,
                user_id=target,
                capabilities=["discover", "read"],
                base_revision=0,
                actor_id=owner,
            )

        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_until=NULL "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {"o": ontology_id, "u": owner})
        session.execute(text("UPDATE users SET role='viewer' WHERE id=:u"), {"u": owner})
        with pytest.raises(GrantDenied):
            create_grant(
                session,
                ontology_id=ontology_id,
                user_id=target,
                capabilities=["discover", "read"],
                base_revision=0,
                actor_id=owner,
            )


def test_zz_discoverable_ontology_ids_filter_capability_window_domain_and_role(access_db):
    from app.models.user import User
    from app.services.ontology_access import (
        create_grant,
        creator_grant,
        discoverable_ontology_ids,
    )

    Session, _ = access_db
    owner = _insert_user(Session, "discover-owner", role="admin")
    viewer = _insert_user(Session, "discover-viewer", role="viewer")
    same = _insert_ontology(Session, "Discover same", owner)
    no_discover = _insert_ontology(Session, "Discover no capability", owner)
    expired = _insert_ontology(Session, "Discover expired", owner)
    future = _insert_ontology(Session, "Discover future", owner)

    with Session() as session:
        creator_grant(session, same, session.get(User, owner))
        creator_grant(session, no_discover, session.get(User, owner))
        creator_grant(session, expired, session.get(User, owner))
        creator_grant(session, future, session.get(User, owner))
        for oid, capabilities in (
            (same, ["discover", "read"]),
            (no_discover, ["read"]),
            (expired, ["discover", "read"]),
            (future, ["discover", "read"]),
        ):
            create_grant(
                session,
                ontology_id=oid,
                user_id=viewer,
                capabilities=capabilities,
                base_revision=0,
                actor_id=owner,
            )
        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_until=:when "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {"when": datetime.now(timezone.utc) - timedelta(seconds=1), "o": expired, "u": viewer})
        session.execute(text(
            "UPDATE ontology_project_access_grants SET valid_from=:when "
            "WHERE ontology_id=:o AND user_id=:u"
        ), {"when": datetime.now(timezone.utc) + timedelta(minutes=5), "o": future, "u": viewer})
        actor = session.get(User, viewer)
        ids = session.execute(discoverable_ontology_ids(session, actor)).scalars().all()

    assert ids == [same]
