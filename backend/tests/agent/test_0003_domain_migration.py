import importlib.util
import os
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_DIR / "alembic" / "versions" / "0003_publication_governance.py"
SECURITY_DOMAIN_MODEL = BACKEND_DIR / "app" / "models" / "security_domain.py"
AUTH_REFRESH_MODEL = BACKEND_DIR / "app" / "models" / "auth_refresh.py"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_DATABASE_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL")
DEFAULT_DOMAIN_ID = "00000000-0000-0000-0000-000000000001"
FAMILY_ID = "44444444-4444-4444-4444-444444444444"
TOKEN_ID = "55555555-5555-5555-5555-555555555555"
CROSS_FAMILY_ID = "66666666-6666-6666-6666-666666666666"


def test_p1a_domain_red_contract():
    missing = [
        path
        for path in [MIGRATION, SECURITY_DOMAIN_MODEL, AUTH_REFRESH_MODEL]
        if not path.exists()
    ]
    if missing:
        pytest.fail(
            "RED_P1A_DOMAIN: domain migration foundation missing: "
            + ", ".join(str(path.relative_to(BACKEND_DIR)) for path in missing)
        )


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
def migration_schema():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "p1a_domain_" + uuid.uuid4().hex
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield schema
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _connection(schema):
    return create_engine(_scoped_url(schema))


def test_pgcrypto_preflight_runs_before_domain_ddl(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Result:
        def one_or_none(self):
            return None

    class Bind:
        def execute(self, statement, parameters=None):
            return Result()

    monkeypatch.setattr(module.op, "get_bind", lambda: Bind())
    called = []
    monkeypatch.setattr(module, "upgrade_domain_foundation", lambda: called.append(True))
    with pytest.raises(RuntimeError, match="PGCRYPTO_REQUIRED"):
        module.upgrade()
    assert called == []


def test_pgcrypto_check_expression_failure_prevents_domain_ddl(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_0003_bad_digest", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Result:
        def __init__(self, value):
            self.value = value

        def one_or_none(self):
            return self.value

        def scalar_one(self):
            return self.value

    class Bind:
        dialect = type("Dialect", (), {"identifier_preparer": type("Preparer", (), {"quote": staticmethod(lambda value: value)})()})()

        def __init__(self):
            self.calls = 0

        def execute(self, statement, parameters=None):
            self.calls += 1
            values = (
                type("Digest", (), {"nspname": "public", "oid": 1})(),
                True,
                False,
            )
            return Result(values[self.calls - 1])

    monkeypatch.setattr(module.op, "get_bind", lambda: Bind())
    called = []
    monkeypatch.setattr(module, "upgrade_domain_foundation", lambda: called.append(True))
    with pytest.raises(RuntimeError, match="PGCRYPTO_DIGEST_PRIVILEGE_REQUIRED"):
        module.upgrade()
    assert called == []


def test_real_pgcrypto_absent_revoked_and_owner_bootstrap_are_isolated():
    if not TEST_DATABASE_ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL required for disposable pgcrypto fixture")
    suffix = uuid.uuid4().hex
    database = "p1a_pgcrypto_" + suffix
    owner_role = "p1a_owner_" + suffix
    migration_role = "p1a_migration_" + suffix
    owner_password = uuid.uuid4().hex
    migration_password = uuid.uuid4().hex
    admin = create_engine(TEST_DATABASE_ADMIN_URL, isolation_level="AUTOCOMMIT")
    database_url = make_url(TEST_DATABASE_URL).set(
        database=database, username=migration_role, password=migration_password
    )
    admin_database_url = make_url(TEST_DATABASE_ADMIN_URL).set(database=database)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE ROLE "{owner_role}" LOGIN PASSWORD :password'), {"password": owner_password})
            connection.execute(text(f'CREATE ROLE "{migration_role}" LOGIN PASSWORD :password'), {"password": migration_password})
            connection.execute(text(f'CREATE DATABASE "{database}" OWNER "{owner_role}"'))
        database_admin = create_engine(admin_database_url, isolation_level="AUTOCOMMIT")
        with database_admin.connect() as connection:
            connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{migration_role}"'))
            connection.execute(text(f'GRANT USAGE, CREATE ON SCHEMA public TO "{migration_role}"'))
        result_0002 = subprocess.run(
            [sys.executable, "scripts/run_migrations.py", "upgrade", "0002_entity_identifiers"],
            cwd=BACKEND_DIR,
            env=dict(os.environ, DATABASE_URL=database_url.render_as_string(hide_password=False)),
            capture_output=True,
            text=True,
        )
        assert result_0002.returncode == 0, result_0002.stderr
        result_0003 = subprocess.run(
            [sys.executable, "scripts/run_migrations.py", "upgrade", "0003_publication_governance"],
            cwd=BACKEND_DIR,
            env=dict(os.environ, DATABASE_URL=database_url.render_as_string(hide_password=False)),
            capture_output=True,
            text=True,
        )
        assert result_0003.returncode != 0
        assert "PGCRYPTO_REQUIRED" in result_0003.stderr
        migration_engine = create_engine(database_url)
        with migration_engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_entity_identifiers"
            assert not inspect(connection).has_table("security_domains")

        with database_admin.connect() as connection:
            connection.execute(text("CREATE EXTENSION pgcrypto"))
            connection.execute(text("REVOKE EXECUTE ON FUNCTION public.digest(bytea,text) FROM PUBLIC"))
        result_revoked = subprocess.run(
            [sys.executable, "scripts/run_migrations.py", "upgrade", "0003_publication_governance"],
            cwd=BACKEND_DIR,
            env=dict(os.environ, DATABASE_URL=database_url.render_as_string(hide_password=False)),
            capture_output=True,
            text=True,
        )
        assert result_revoked.returncode != 0
        assert "PGCRYPTO_DIGEST_PRIVILEGE_REQUIRED" in result_revoked.stderr
        with migration_engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_entity_identifiers"
            assert not inspect(connection).has_table("security_domains")

        with database_admin.connect() as connection:
            connection.execute(text(f'GRANT EXECUTE ON FUNCTION public.digest(bytea,text) TO "{migration_role}"'))
        result_success = subprocess.run(
            [sys.executable, "scripts/run_migrations.py", "upgrade", "0003_publication_governance"],
            cwd=BACKEND_DIR,
            env=dict(os.environ, DATABASE_URL=database_url.render_as_string(hide_password=False)),
            capture_output=True,
            text=True,
        )
        assert result_success.returncode == 0, result_success.stderr
        with migration_engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003_publication_governance"
            assert inspect(connection).has_table("security_domains")
        migration_engine.dispose()
        database_admin.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{migration_role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{owner_role}"'))
        admin.dispose()


def test_populated_0002_upgrade_legacy_writes_isolation_and_downgrade(migration_schema):
    _alembic(migration_schema, "upgrade", "0002_entity_identifiers")
    engine = _connection(migration_schema)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('legacy-user','legacy','legacy@example.com','hash','viewer',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('legacy-admin','legacy-admin','legacy-admin@example.com','hash','admin',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at) "
            "VALUES ('legacy-ontology','Legacy','test','v0.1','draft','legacy-user',now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at) "
            "VALUES ('legacy-admin-ontology','Legacy Admin','test','v0.1','draft','legacy-admin',now(),now())"
        ))
    engine.dispose()

    _alembic(migration_schema, "upgrade", "0003_publication_governance")
    engine = _connection(migration_schema)
    inspector = inspect(engine)
    assert {"security_domains", "auth_refresh_families", "auth_refresh_tokens"} <= set(inspector.get_table_names())
    assert "security_domain_id" in {column["name"] for column in inspector.get_columns("users")}

    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM security_domains WHERE id=:id AND key='default' AND status='active'"), {"id": DEFAULT_DOMAIN_ID}).scalar_one() == 1
        assert connection.execute(text("SELECT security_domain_id FROM users WHERE id='legacy-user'")).scalar_one() == DEFAULT_DOMAIN_ID
        assert connection.execute(text("SELECT security_domain_id FROM ontology_projects WHERE id='legacy-ontology'")).scalar_one() == DEFAULT_DOMAIN_ID

        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('old-user','old','old@example.com','hash','viewer',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at,security_domain_id) "
            "VALUES ('null-user','null','null@example.com','hash','editor',true,now(),now(),NULL)"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at) "
            "VALUES ('old-ontology','Old','test','v0.1','draft','old-user',now(),now())"
        ))
        assert connection.execute(text("SELECT count(*) FROM users WHERE security_domain_id=:id"), {"id": DEFAULT_DOMAIN_ID}).scalar_one() == 4

    with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="SECURITY_DOMAIN_MISMATCH"):
            connection.execute(text(
                "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at,security_domain_id) "
                "VALUES ('bad-user','bad','bad@example.com','hash','viewer',true,now(),now(),'11111111-1111-1111-1111-111111111111')"
            ))
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO security_domains (id,key,status) VALUES ('22222222-2222-2222-2222-222222222222','other','active')"
            ))
    with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="SECURITY_DOMAIN_IMMUTABLE"):
            connection.execute(text("UPDATE security_domains SET status='inactive' WHERE id=:id"), {"id": DEFAULT_DOMAIN_ID})

    _alembic(migration_schema, "downgrade", "0002_entity_identifiers")
    inspector = inspect(engine)
    assert "security_domains" not in inspector.get_table_names()
    assert "security_domain_id" not in {column["name"] for column in inspector.get_columns("users")}
    with engine.connect() as connection:
        assert "pgcrypto" in {row[0] for row in connection.execute(text("SELECT extname FROM pg_extension"))}
    engine.dispose()

    _alembic(migration_schema, "upgrade", "0003_publication_governance")
    engine = _connection(migration_schema)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT security_domain_id FROM users WHERE id='legacy-user'")).scalar_one() == DEFAULT_DOMAIN_ID
    engine.dispose()


def test_unchanged_legacy_application_writers_receive_default_domain(migration_schema):
    from app.models.user import User
    from app.routers.auth import register
    from app.routers.ontologies import create_ontology
    from app.routers.users import create_user
    from app.schemas.auth import RegisterRequest
    from app.schemas.ontology import OntologyCreate
    from app.schemas.user import UserCreate
    from app.services.auth_service import seed_admin

    _alembic(migration_schema, "upgrade", "0003_publication_governance")
    engine = _connection(migration_schema)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        seed_admin(session)
        register(None, RegisterRequest(username="registered", email="registered@example.com", password="secret123"), session)
        create_user(UserCreate(username="ordinary", email="ordinary@example.com", password="secret123", role="viewer"), session, object())
        editor_receipt = create_user(UserCreate(username="editor-old", email="editor-old@example.com", password="secret123", role="editor"), session, object())
        editor = session.get(User, editor_receipt["data"]["id"])
        create_ontology(OntologyCreate(name="Legacy writer ontology", domain="其他"), session, editor)

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at,security_domain_id) "
            "VALUES ('explicit-null-user','explicit-null','explicit-null@example.com','hash','viewer',true,now(),now(),NULL)"
        ))
        connection.execute(text(
            "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at,security_domain_id) "
            "VALUES ('explicit-null-ontology','Explicit null','test','v0.1','draft','explicit-null-user',now(),now(),NULL)"
        ))
        assert connection.execute(text("SELECT count(*) FROM users WHERE security_domain_id <> :domain OR security_domain_id IS NULL"), {"domain": DEFAULT_DOMAIN_ID}).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM ontology_projects WHERE security_domain_id <> :domain OR security_domain_id IS NULL"), {"domain": DEFAULT_DOMAIN_ID}).scalar_one() == 0
    engine.dispose()


def test_domain_helper_failure_rolls_back_all_schema_mutation(migration_schema):
    _alembic(migration_schema, "upgrade", "0002_entity_identifiers")
    engine = _connection(migration_schema)
    with engine.begin() as connection:
        connection.execute(text("CREATE FUNCTION enforce_default_security_domain() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"))
    result = _alembic(migration_schema, "upgrade", "0003_publication_governance", check=False)
    assert result.returncode != 0
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_entity_identifiers"
        assert not inspect(connection).has_table("security_domains")
        assert "security_domain_id" not in {column["name"] for column in inspect(connection).get_columns("users")}
    engine.dispose()


def test_fresh_upgrade_refresh_domain_fk_and_append_only(migration_schema):
    _alembic(migration_schema, "upgrade", "0003_publication_governance")
    engine = _connection(migration_schema)
    second_domain = "33333333-3333-3333-3333-333333333333"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('user-a','usera','usera@example.com','hash','viewer',true,now(),now())"
        ))
        connection.execute(text(
            "INSERT INTO auth_refresh_families (id,user_id,security_domain_id,current_generation,status,expires_at) "
            "VALUES (:family,'user-a',:domain,0,'active',now()+interval '1 day')"
        ), {"family": FAMILY_ID, "domain": DEFAULT_DOMAIN_ID})
        connection.execute(text(
            "INSERT INTO auth_refresh_tokens (id,family_id,generation,token_hash,status) "
            "VALUES (:token,:family,0,'sha256-only','active')"
        ), {"token": TOKEN_ID, "family": FAMILY_ID})

    with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="AUTH_REFRESH_TOKEN_APPEND_ONLY"):
            connection.execute(text("UPDATE auth_refresh_tokens SET status='used' WHERE id=:token"), {"token": TOKEN_ID})
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE security_domains DISABLE TRIGGER security_domains_immutable"))
        connection.execute(text(
            "INSERT INTO security_domains (id,key,status) VALUES (:id,'fixture','inactive')"
        ), {"id": second_domain})
        connection.execute(text("ALTER TABLE security_domains ENABLE TRIGGER security_domains_immutable"))
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO auth_refresh_families (id,user_id,security_domain_id,current_generation,status,expires_at) "
                "VALUES (:family,'user-a',:domain,0,'active',now()+interval '1 day')"
            ), {"family": CROSS_FAMILY_ID, "domain": second_domain})
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users DISABLE TRIGGER users_default_security_domain"))
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at,security_domain_id) "
            "VALUES ('fixture-user','fixture','fixture@example.com','hash','viewer',true,now(),now(),:domain)"
        ), {"domain": second_domain})
        connection.execute(text("ALTER TABLE users ENABLE TRIGGER users_default_security_domain"))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO ontology_projects (id,name,domain,version,status,created_by,created_at,updated_at,security_domain_id) "
                "VALUES ('cross-ontology','Cross','test','v0.1','draft','fixture-user',now(),now(),:default_domain)"
            ), {"default_domain": DEFAULT_DOMAIN_ID})
    with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="AUTH_REFRESH_TOKEN_APPEND_ONLY"):
            connection.execute(text("DELETE FROM auth_refresh_tokens WHERE id=:token"), {"token": TOKEN_ID})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("DELETE FROM auth_refresh_families WHERE id=:family"), {"family": FAMILY_ID})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("DELETE FROM users WHERE id='user-a'"))
    token_columns = {column["name"] for column in inspect(engine).get_columns("auth_refresh_tokens")}
    assert token_columns == {"id", "family_id", "generation", "token_hash", "status", "issued_at", "used_at"}
    family_fks = {fk["name"]: fk for fk in inspect(engine).get_foreign_keys("auth_refresh_families")}
    token_fks = {fk["name"]: fk for fk in inspect(engine).get_foreign_keys("auth_refresh_tokens")}
    assert family_fks["fk_auth_refresh_family_user_domain"]["options"]["ondelete"] == "RESTRICT"
    assert token_fks["fk_auth_refresh_token_family"]["options"]["ondelete"] == "RESTRICT"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM auth_refresh_families WHERE id=:family"), {"family": FAMILY_ID}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM auth_refresh_tokens WHERE id=:token"), {"token": TOKEN_ID}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM users WHERE id='user-a'")).scalar_one() == 1
    engine.dispose()


def test_canonical_uuid_checks_reject_malformed_and_uppercase_ids(migration_schema):
    _alembic(migration_schema, "upgrade", "0003_publication_governance")
    engine = _connection(migration_schema)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO security_domains (id,key,status) VALUES ('not-a-uuid','bad','inactive')"))
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,username,email,password_hash,role,is_active,created_at,updated_at) "
            "VALUES ('uuid-user','uuid-user','uuid-user@example.com','hash','viewer',true,now(),now())"
        ))
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO auth_refresh_families (id,user_id,security_domain_id,current_generation,status,expires_at) "
                "VALUES ('AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA','uuid-user',:domain,0,'active',now()+interval '1 day')"
            ), {"domain": DEFAULT_DOMAIN_ID})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO auth_refresh_families (id,user_id,security_domain_id,current_generation,status,expires_at) "
            "VALUES (:family,'uuid-user',:domain,0,'active',now()+interval '1 day')"
        ), {"family": FAMILY_ID, "domain": DEFAULT_DOMAIN_ID})
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO auth_refresh_tokens (id,family_id,generation,token_hash,status) "
                "VALUES ('BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB',:family,0,'hash','active')"
            ), {"family": FAMILY_ID})
    engine.dispose()


def test_zz_models_expose_exact_domain_and_hash_only_refresh_contract():
    from app.models.auth_refresh import AuthRefreshFamily, AuthRefreshToken
    from app.models.security_domain import SecurityDomain

    assert set(SecurityDomain.__table__.c) >= {SecurityDomain.__table__.c.id, SecurityDomain.__table__.c.key}
    token_columns = set(AuthRefreshToken.__table__.c.keys())
    assert token_columns == {"id", "family_id", "generation", "token_hash", "status", "issued_at", "used_at"}
    assert not {"token", "refresh_token", "bearer"} & token_columns
    family_fks = {constraint.name for constraint in AuthRefreshFamily.__table__.foreign_key_constraints}
    assert "fk_auth_refresh_family_user_domain" in family_fks
    assert {constraint.name for constraint in SecurityDomain.__table__.constraints} >= {"ck_security_domains_id_uuid"}
    assert {constraint.name for constraint in AuthRefreshFamily.__table__.constraints} >= {"ck_auth_refresh_families_id_uuid"}
    assert {constraint.name for constraint in AuthRefreshToken.__table__.constraints} >= {"ck_auth_refresh_tokens_id_uuid"}
