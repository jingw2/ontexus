"""OntologyProjectAccessGrant service and 0003 access-foundation helper.

The grant is the sole design/lifecycle authority for an Ontology, with the
closed capability vocabulary `discover|read|edit|publish`, exact role ceilings,
monotonic-revision CAS create/revise/revoke, no self-escalation, audited
owner recovery, and existence-hiding denials.

`upgrade_access_foundation()`/`downgrade_access_foundation()` create the grant
table, add the `working_revision` column on `ontology_projects` and the
`revision` column on `ontology_migration_findings` (both required by the P1A
remediation/recovery CAS), and backfill creator grants plus
`ONTOLOGY_OWNER_RECOVERY_REQUIRED` findings for every existing Ontology.
"""
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.ontology_access import OntologyProjectAccessGrant
from app.models.ontology import OntologyProject
from app.models.user import User
from app.services.auth_service import get_user_by_id
from app.services.governance_audit import enqueue_audit

CAPABILITIES = ("discover", "read", "edit", "publish")
ROLE_CEILINGS = {
    "viewer": {"discover", "read"},
    "editor": set(CAPABILITIES),
    "admin": set(CAPABILITIES),
}
UUID_CHECK = (
    "VALUE ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$'"
)
CAPABILITIES_JSON = '["discover", "read", "edit", "publish"]'


class GrantDenied(Exception):
    """Known but unauthorized (existence-hiding) grant failure."""


class GrantConflict(Exception):
    """Optimistic/concurrency or state-conflict grant failure."""


def validate_capabilities(capabilities) -> list[str]:
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("capabilities must be a non-empty array")
    values = [value for value in capabilities]
    if any(value not in CAPABILITIES for value in values) or len(set(values)) != len(values):
        raise ValueError(f"capabilities must be a subset of {list(CAPABILITIES)}")
    return values


def _ontology_domain(db: Session, ontology_id: str) -> str | None:
    return db.execute(
        sa.text("SELECT security_domain_id FROM ontology_projects WHERE id = :id"),
        {"id": ontology_id},
    ).scalar_one_or_none()


def _enqueue_audit(db: Session, *, security_domain_id: str, correlation_id: str,
                   operation: str, decision: str, outcome: str, actor_user_id: str) -> None:
    enqueue_audit(
        db.connection(),
        security_domain_id=security_domain_id,
        correlation_id=correlation_id,
        operation=operation,
        decision=decision,
        outcome=outcome,
        actor_user_id=actor_user_id,
        retention_class="standard",
    )


def current_grant(db: Session, ontology_id: str, user_id: str) -> dict | None:
    row = db.execute(
        sa.text(
            "SELECT id, ontology_id, user_id, security_domain_id, capabilities, revision, status, created_by, "
            "valid_from, valid_until, revoked_by, created_at, updated_at, revoked_at "
            "FROM ontology_project_access_grants "
            "WHERE ontology_id = :o AND user_id = :u AND status = 'active'"
        ),
        {"o": ontology_id, "u": user_id},
    ).mappings().one_or_none()
    return _row_dict(row) if row is not None else None


def _row_dict(row) -> dict:
    result = dict(row)
    capabilities = result["capabilities"]
    # `capabilities` is a JSON column; a raw `text()` SELECT (used throughout
    # this module) round-trips it as a Python list on PostgreSQL but as a
    # raw JSON-encoded string on SQLite — `list("...")` on that string would
    # silently iterate individual characters instead of the real
    # capabilities, which made every capability check fail closed for the
    # wrong reason (confirmed directly: `require_project_grant` denying
    # "edit" for a grant that genuinely holds it). Decode explicitly rather
    # than indexing into the raw `'['` character.
    if isinstance(capabilities, (str, bytes, bytearray)):
        import json

        capabilities = json.loads(capabilities) if capabilities else []
    result["capabilities"] = list(capabilities)
    return result


def _grant_by_id(db: Session, grant_id: str) -> dict | None:
    row = db.execute(
        sa.text(
            "SELECT id, ontology_id, user_id, capabilities, revision, status, created_by, "
            "valid_from, valid_until, revoked_by, created_at, updated_at, revoked_at "
            "FROM ontology_project_access_grants WHERE id = :id"
        ),
        {"id": grant_id},
    ).mappings().one_or_none()
    return _row_dict(row) if row is not None else None


def creator_grant(db: Session, ontology_id: str, user) -> dict:
    """Exact creator grant capped by the creator's role ceiling (bootstrap path).

    Called atomically when an Ontology is created (P1B wires it into the
    ontology creation transaction); the creator has no grant yet, so the
    actor/self-escalation checks do not apply.
    """
    ceiling = ROLE_CEILINGS.get(user.role, ROLE_CEILINGS["viewer"])
    grant = OntologyProjectAccessGrant(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        user_id=user.id,
        security_domain_id=user.security_domain_id,
        capabilities=sorted(ceiling),
        revision=1,
        status="active",
        created_by=user.id,
    )
    db.add(grant)
    db.flush()
    _enqueue_audit(
        db, security_domain_id=user.security_domain_id, correlation_id=f"grant:{grant.id}:create",
        operation="ontology.access-grant.creator", decision="allow", outcome="succeeded",
        actor_user_id=user.id,
    )
    db.commit()
    return _row_dict(current_grant(db, ontology_id, user.id))


def backfill_creator_grants(db: Session) -> dict:
    """Repair path: insert missing creator grants for Ontology rows.

    Idempotent bulk repair mirroring the `upgrade_access_foundation()` 0003
    backfill for rows that predate the access foundation or were written by
    direct-SQL flows (migration scripts / seeded test data) that bypass the
    `create_ontology` router and therefore never received `creator_grant`.
    Active editor/admin creators receive all four capabilities; active viewer
    creators receive discover|read; inactive/missing creators receive an
    `ONTOLOGY_OWNER_RECOVERY_REQUIRED` finding.  Requires a schema that
    already has the grant table (0003+) — run migrations first.
    """
    try:
        db.execute(sa.text("SELECT 1 FROM ontology_project_access_grants LIMIT 1"))
    except sa.exc.ProgrammingError:
        db.rollback()
        raise RuntimeError(
            "ontology_project_access_grants is missing — run migrations to at least "
            "0003_publication_governance (scripts/run_migrations.py upgrade head) first"
        ) from None
    grants_editor = db.execute(sa.text(
        """
        INSERT INTO ontology_project_access_grants
          (id, ontology_id, user_id, security_domain_id, capabilities, revision, status,
           created_by, created_at, updated_at)
        SELECT gen_random_uuid()::text, o.id, u.id, u.security_domain_id,
               '["discover", "read", "edit", "publish"]'::jsonb, 1, 'active',
               o.created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM ontology_projects o
        JOIN users u ON u.id = o.created_by
        WHERE u.role IN ('editor', 'admin') AND u.is_active
        ON CONFLICT (ontology_id, user_id) DO NOTHING
        """
    ))
    grants_viewer = db.execute(sa.text(
        """
        INSERT INTO ontology_project_access_grants
          (id, ontology_id, user_id, security_domain_id, capabilities, revision, status,
           created_by, created_at, updated_at)
        SELECT gen_random_uuid()::text, o.id, u.id, u.security_domain_id,
               '["discover", "read"]'::jsonb, 1, 'active',
               o.created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM ontology_projects o
        JOIN users u ON u.id = o.created_by
        WHERE u.role = 'viewer' AND u.is_active
        ON CONFLICT (ontology_id, user_id) DO NOTHING
        """
    ))
    findings = db.execute(sa.text(
        """
        INSERT INTO ontology_migration_findings
          (id, ontology_id, entity_id, kind, item_id, code, path, message, source_hash,
           classification, status, revision)
        SELECT gen_random_uuid()::text, o.id, NULL, 'owner', o.id,
               'ONTOLOGY_OWNER_RECOVERY_REQUIRED',
               'ontologies/' || o.id,
               'The ontology creator is a viewer, inactive, or missing; an admin must assign an active editor/admin owner',
               digest(o.id::bytea, 'sha256'), NULL, 'open', 1
        FROM ontology_projects o
        LEFT JOIN users u ON u.id = o.created_by
        WHERE (u.id IS NULL OR NOT u.is_active OR u.role = 'viewer')
          AND NOT EXISTS (
              SELECT 1 FROM ontology_migration_findings f
              WHERE f.ontology_id = o.id AND f.kind = 'owner'
                AND f.code = 'ONTOLOGY_OWNER_RECOVERY_REQUIRED'
          )
        """
    ))
    db.commit()
    return {
        "editor_or_admin_grants": grants_editor.rowcount,
        "viewer_grants": grants_viewer.rowcount,
        "grant_rows_inserted": grants_editor.rowcount + grants_viewer.rowcount,
        "owner_recovery_findings_inserted": findings.rowcount,
    }


def _require_actor(db: Session, actor_id: str, ontology_id: str) -> dict:
    actor = get_user_by_id(db, actor_id)
    if actor is None:
        raise GrantDenied("actor required")
    return require_project_grant(db, actor, ontology_id, "edit")


def _check_ceiling(role: str, capabilities: list[str]) -> None:
    ceiling = ROLE_CEILINGS.get(role, ROLE_CEILINGS["viewer"])
    if not set(capabilities) <= ceiling:
        raise GrantDenied("capabilities exceed the recipient role ceiling")


def create_grant(db: Session, *, ontology_id: str, user_id: str, capabilities,
                 base_revision: int, actor_id: str) -> dict:
    capabilities = validate_capabilities(capabilities)
    if user_id == actor_id:
        raise GrantDenied("an actor may not create their own grant")
    _require_actor(db, actor_id, ontology_id)
    domain = _ontology_domain(db, ontology_id)
    if domain is None:
        raise GrantDenied("ontology not found")
    recipient = get_user_by_id(db, user_id)
    if recipient is None:
        raise GrantDenied("recipient not found")
    _check_ceiling(recipient.role, capabilities)
    existing = db.execute(
        sa.text(
            "SELECT id FROM ontology_project_access_grants "
            "WHERE ontology_id = :o AND user_id = :u FOR UPDATE"
        ),
        {"o": ontology_id, "u": user_id},
    ).scalar_one_or_none()
    if existing is not None:
        raise GrantConflict("GRANT_ALREADY_EXISTS")
    grant = OntologyProjectAccessGrant(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        user_id=user_id,
        security_domain_id=domain,
        capabilities=capabilities,
        revision=1,
        status="active",
        created_by=actor_id,
    )
    db.add(grant)
    db.flush()
    _enqueue_audit(
        db, security_domain_id=domain, correlation_id=f"grant:{grant.id}:create",
        operation="ontology.access-grant.create", decision="allow", outcome="succeeded",
        actor_user_id=actor_id,
    )
    db.commit()
    return _row_dict(current_grant(db, ontology_id, user_id))


def revise_grant(db: Session, *, grant_id: str, capabilities, base_revision: int, actor_id: str) -> dict:
    capabilities = validate_capabilities(capabilities)
    row = db.execute(
        sa.text(
            "SELECT id, ontology_id, user_id, revision, status, security_domain_id "
            "FROM ontology_project_access_grants WHERE id = :id FOR UPDATE"
        ),
        {"id": grant_id},
    ).mappings().one_or_none()
    if row is None:
        raise GrantConflict("GRANT_NOT_FOUND")
    if row["user_id"] == actor_id:
        raise GrantDenied("an actor may not revise their own grant")
    if row["status"] != "active":
        raise GrantConflict("GRANT_NOT_ACTIVE")
    if row["revision"] != base_revision:
        raise GrantConflict("GRANT_REVISION_CONFLICT")
    _require_actor(db, actor_id, row["ontology_id"])
    recipient = get_user_by_id(db, row["user_id"])
    if recipient is None:
        raise GrantDenied("recipient not found")
    _check_ceiling(recipient.role, capabilities)
    db.execute(
        sa.text(
            "UPDATE ontology_project_access_grants SET capabilities = CAST(:capabilities AS jsonb), "
            "revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
        ),
        {"capabilities": _json_list(capabilities), "id": grant_id},
    )
    _enqueue_audit(
        db, security_domain_id=row["security_domain_id"], correlation_id=f"grant:{grant_id}:revise",
        operation="ontology.access-grant.revise", decision="allow", outcome="succeeded",
        actor_user_id=actor_id,
    )
    db.commit()
    return _row_dict(current_grant(db, row["ontology_id"], row["user_id"]))


def revoke_grant(db: Session, *, grant_id: str, base_revision: int, actor_id: str) -> dict:
    row = db.execute(
        sa.text(
            "SELECT id, ontology_id, user_id, revision, status, security_domain_id "
            "FROM ontology_project_access_grants WHERE id = :id FOR UPDATE"
        ),
        {"id": grant_id},
    ).mappings().one_or_none()
    if row is None:
        raise GrantConflict("GRANT_NOT_FOUND")
    if row["user_id"] == actor_id:
        raise GrantDenied("an actor may not revoke their own grant")
    if row["status"] != "active":
        raise GrantConflict("GRANT_NOT_ACTIVE")
    if row["revision"] != base_revision:
        raise GrantConflict("GRANT_REVISION_CONFLICT")
    _require_actor(db, actor_id, row["ontology_id"])
    db.execute(
        sa.text(
            "UPDATE ontology_project_access_grants SET status = 'revoked', revision = revision + 1, "
            "revoked_by = :actor, revoked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"
        ),
        {"actor": actor_id, "id": grant_id},
    )
    _enqueue_audit(
        db, security_domain_id=row["security_domain_id"], correlation_id=f"grant:{grant_id}:revoke",
        operation="ontology.access-grant.revoke", decision="allow", outcome="succeeded",
        actor_user_id=actor_id,
    )
    db.commit()
    return _grant_by_id(db, grant_id)


def recover_owner(db: Session, *, ontology_id: str, base_finding_revision: int,
                  assignee_user_id: str, actor_id: str) -> dict:
    actor = get_user_by_id(db, actor_id)
    if actor is None or actor.role != "admin":
        raise GrantDenied("admin role required for owner recovery")
    finding = db.execute(
        sa.text(
            "SELECT id, revision, status FROM ontology_migration_findings "
            "WHERE ontology_id = :o AND kind = 'owner' AND code = 'ONTOLOGY_OWNER_RECOVERY_REQUIRED' "
            "FOR UPDATE"
        ),
        {"o": ontology_id},
    ).mappings().one_or_none()
    if finding is None or finding["status"] != "open":
        raise GrantConflict("RECOVERY_FINDING_NOT_OPEN")
    if finding["revision"] != base_finding_revision:
        raise GrantConflict("RECOVERY_FINDING_REVISION_CONFLICT")
    assignee = get_user_by_id(db, assignee_user_id)
    if assignee is None or not assignee.is_active or assignee.role not in ("editor", "admin"):
        raise GrantDenied("assignee must be an active editor/admin")
    domain = _ontology_domain(db, ontology_id)
    if domain is None:
        raise GrantDenied("ontology not found")
    slot = db.execute(
        sa.text(
            "SELECT id, revision FROM ontology_project_access_grants "
            "WHERE ontology_id = :o AND user_id = :u FOR UPDATE"
        ),
        {"o": ontology_id, "u": assignee_user_id},
    ).mappings().one_or_none()
    if slot is None:
        db.add(OntologyProjectAccessGrant(
            id=str(uuid.uuid4()),
            ontology_id=ontology_id,
            user_id=assignee_user_id,
            security_domain_id=domain,
            capabilities=list(CAPABILITIES),
            revision=1,
            status="active",
            created_by=actor_id,
        ))
    else:
        status = db.execute(
            sa.text("SELECT status FROM ontology_project_access_grants WHERE id = :id"),
            {"id": slot["id"]},
        ).scalar_one()
        if status == "active":
            raise GrantConflict("OWNER_ALREADY_ASSIGNED")
        db.execute(
            sa.text(
                "UPDATE ontology_project_access_grants SET status = 'active', "
                "capabilities = CAST(:capabilities AS jsonb), revision = revision + 1, "
                "revoked_by = NULL, revoked_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {"capabilities": _json_list(CAPABILITIES), "id": slot["id"]},
        )
    db.execute(
        sa.text(
            "UPDATE ontology_migration_findings SET status = 'resolved', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"
        ),
        {"id": finding["id"]},
    )
    _enqueue_audit(
        db, security_domain_id=domain, correlation_id=f"grant:recovery:{ontology_id}",
        operation="ontology.owner-recovery.assign", decision="allow", outcome="succeeded",
        actor_user_id=actor_id,
    )
    db.commit()
    return _row_dict(current_grant(db, ontology_id, assignee_user_id))


def require_project_grant(db: Session, actor, ontology_id: str, capability: str) -> dict:
    if actor is None or not getattr(actor, "id", None) or not getattr(actor, "is_active", False):
        raise GrantDenied("actor required")

    if capability not in CAPABILITIES:
        raise GrantDenied("unknown capability")

    row = db.execute(
        sa.text(
            "SELECT g.id, g.ontology_id, g.user_id, g.security_domain_id, g.capabilities, "
            "g.revision, g.status, g.created_by, g.valid_from, g.valid_until, g.revoked_by, "
            "g.created_at, g.updated_at, g.revoked_at, "
            "o.security_domain_id AS ontology_security_domain_id, "
            "u.security_domain_id AS actor_security_domain_id, u.is_active AS actor_is_active, "
            "u.role AS actor_role "
            "FROM ontology_project_access_grants g "
            "JOIN ontology_projects o ON o.id = g.ontology_id "
            "JOIN users u ON u.id = g.user_id "
            "WHERE g.ontology_id = :o AND g.user_id = :u"
        ),
        {
            "o": ontology_id,
            "u": actor.id,
        },
    ).mappings().one_or_none()
    if row is None:
        raise GrantDenied("grant not held")

    grant = _row_dict(row)
    now = datetime.now(timezone.utc)
    valid_from = _as_utc(grant["valid_from"])
    valid_until = _as_utc(grant["valid_until"])
    actor_domain = getattr(actor, "security_domain_id", None)
    if (
        not row["actor_is_active"]
        or row["actor_security_domain_id"] != actor_domain
        or row["ontology_security_domain_id"] != actor_domain
        or grant["security_domain_id"] != actor_domain
        or grant["status"] != "active"
        or (valid_from is not None and valid_from > now)
        or (valid_until is not None and now >= valid_until)
        or capability not in grant["capabilities"]
        or capability not in ROLE_CEILINGS.get(row["actor_role"], ROLE_CEILINGS["viewer"])
    ):
        raise GrantDenied("grant not held")
    return grant


def _as_utc(value):
    """Treat naive database timestamps as UTC for grant validity checks."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def discoverable_ontology_ids(db: Session, actor):
    """Return a SQL selectable of ontology IDs the actor may discover.

    This intentionally remains a selectable so callers can apply the grant
    predicate before counting, paginating, or aggregating project rows.
    Direct ``read`` grants without ``discover`` remain valid for ID-based
    reads and therefore do not appear here.
    """
    if actor is None or not getattr(actor, "id", None) or not getattr(actor, "is_active", False):
        return sa.select(OntologyProject.id).where(sa.false())

    capability = "discover"
    if capability not in ROLE_CEILINGS.get(getattr(actor, "role", None), ROLE_CEILINGS["viewer"]):
        return sa.select(OntologyProject.id).where(sa.false())

    now = datetime.now(timezone.utc)
    grant = OntologyProjectAccessGrant
    user = User
    ontology = OntologyProject
    return (
        sa.select(ontology.id)
        .join(
            grant,
            sa.and_(
                grant.ontology_id == ontology.id,
                grant.security_domain_id == ontology.security_domain_id,
            ),
        )
        .join(
            user,
            sa.and_(
                user.id == grant.user_id,
                user.security_domain_id == grant.security_domain_id,
            ),
        )
        .where(
            grant.user_id == actor.id,
            grant.security_domain_id == getattr(actor, "security_domain_id", None),
            user.security_domain_id == getattr(actor, "security_domain_id", None),
            user.is_active.is_(True),
            grant.status == "active",
            sa.or_(grant.valid_from.is_(None), grant.valid_from <= now),
            sa.or_(grant.valid_until.is_(None), now < grant.valid_until),
            sa.cast(grant.capabilities, sa.Text).like('%"discover"%'),
        )
        .distinct()
    )


def _json_list(values) -> str:
    import json

    return json.dumps(sorted(values), ensure_ascii=False)


def upgrade_access_foundation() -> None:
    op.add_column(
        "ontology_projects",
        sa.Column("working_revision", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "ontology_migration_findings",
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_ontology_migration_findings_revision",
        "ontology_migration_findings",
        "revision > 0",
    )
    op.create_table(
        "ontology_project_access_grants",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ontology_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("security_domain_id", sa.String(36), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("revoked_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ontology_project_access_grants"),
        sa.CheckConstraint(UUID_CHECK.replace("VALUE", "id"), name="ck_ontology_project_access_grants_id_uuid"),
        sa.CheckConstraint("revision > 0", name="ck_ontology_project_access_grants_revision"),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')",
            name="ck_ontology_project_access_grants_status",
        ),
        sa.CheckConstraint(
            f"jsonb_typeof(capabilities) = 'array' AND capabilities <@ '{CAPABILITIES_JSON}'::jsonb",
            name="ck_ontology_project_access_grants_capabilities",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_from < valid_until",
            name="ck_ontology_project_access_grants_validity",
        ),
        sa.UniqueConstraint("ontology_id", "user_id", name="uq_ontology_project_access_grant_slot"),
        sa.ForeignKeyConstraint(
            ["user_id", "security_domain_id"],
            ["users.id", "users.security_domain_id"],
            ondelete="RESTRICT",
            name="fk_ontology_project_access_grant_user_domain",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_id", "security_domain_id"],
            ["ontology_projects.id", "ontology_projects.security_domain_id"],
            ondelete="RESTRICT",
            name="fk_ontology_project_access_grant_ontology_domain",
        ),
    )
    # creator-grant backfill: editor/admin active creators receive all four
    op.execute(
        sa.text(
            """
            INSERT INTO ontology_project_access_grants
              (id, ontology_id, user_id, security_domain_id, capabilities, revision, status,
               created_by, created_at, updated_at)
            SELECT gen_random_uuid()::text, o.id, u.id, u.security_domain_id,
                   '["discover", "read", "edit", "publish"]'::jsonb, 1, 'active',
                   o.created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM ontology_projects o
            JOIN users u ON u.id = o.created_by
            WHERE u.role IN ('editor', 'admin') AND u.is_active
            ON CONFLICT (ontology_id, user_id) DO NOTHING
            """
        )
    )
    # active viewer creators receive discover|read only
    op.execute(
        sa.text(
            """
            INSERT INTO ontology_project_access_grants
              (id, ontology_id, user_id, security_domain_id, capabilities, revision, status,
               created_by, created_at, updated_at)
            SELECT gen_random_uuid()::text, o.id, u.id, u.security_domain_id,
                   '["discover", "read"]'::jsonb, 1, 'active',
                   o.created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM ontology_projects o
            JOIN users u ON u.id = o.created_by
            WHERE u.role = 'viewer' AND u.is_active
            ON CONFLICT (ontology_id, user_id) DO NOTHING
            """
        )
    )
    # every creator who is not an active editor/admin needs owner recovery
    op.execute(
        sa.text(
            """
            INSERT INTO ontology_migration_findings
              (id, ontology_id, entity_id, kind, item_id, code, path, message, source_hash,
               classification, status, revision)
            SELECT gen_random_uuid()::text, o.id, NULL, 'owner', o.id,
                   'ONTOLOGY_OWNER_RECOVERY_REQUIRED',
                   'ontologies/' || o.id,
                   'The ontology creator is a viewer, inactive, or missing; an admin must assign an active editor/admin owner',
                   digest(o.id::bytea, 'sha256'), NULL, 'open', 1
            FROM ontology_projects o
            LEFT JOIN users u ON u.id = o.created_by
            WHERE (u.id IS NULL OR NOT u.is_active OR u.role = 'viewer')
              AND NOT EXISTS (
                  SELECT 1 FROM ontology_migration_findings f
                  WHERE f.ontology_id = o.id AND f.kind = 'owner'
                    AND f.code = 'ONTOLOGY_OWNER_RECOVERY_REQUIRED'
              )
            """
        )
    )


def downgrade_access_foundation() -> None:
    op.drop_table("ontology_project_access_grants")
    op.drop_constraint("ck_ontology_migration_findings_revision", "ontology_migration_findings", type_="check")
    op.drop_column("ontology_migration_findings", "revision")
    op.drop_column("ontology_projects", "working_revision")
