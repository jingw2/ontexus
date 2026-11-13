"""v2 saved database connection resource and sample API coverage."""
import json
from unittest.mock import MagicMock, patch

from app.models.v2.connection import Connection
from app.models.user import User
from app.routers.v2 import connections as connections_router
from app.services.auth_service import create_access_token, hash_password


def _override_connections_db(db):
    def override():
        yield db

    return override


def test_saved_connection_keeps_credentials_encrypted_and_lists_resources(
    client, db, auth_headers, admin_user, monkeypatch,
):
    app = client.app
    previous = app.dependency_overrides.get(connections_router.get_db)
    app.dependency_overrides[connections_router.get_db] = _override_connections_db(db)
    try:
        created = client.post(
            "/api/v2/connections",
            headers=auth_headers,
            json={
                "name": "runtime mysql",
                "kind": "mysql",
                "config": {
                    "host": "127.0.0.1",
                    "port": "53306",
                    "database": "runtime",
                    "user": "runtime",
                    "password": "secret",
                },
            },
        )
        assert created.status_code == 201
        connection = db.get(Connection, created.json()["id"])
        assert connection is not None
        assert "secret" not in json.dumps(connection.config)
        assert "_encrypted" in connection.config
        assert connection.created_by == admin_user.id

        fake = MagicMock()
        fake.list_resources.return_value = ["codex_pipeline_probe"]
        monkeypatch.setattr(connections_router, "get_connector", lambda kind, config: fake)
        listed = client.get(
            f"/api/v2/connections/{connection.id}/resources",
            headers=auth_headers,
        )

        assert listed.status_code == 200
        assert listed.json()["resources"] == ["codex_pipeline_probe"]
        fake.list_resources.assert_called_once_with()
    finally:
        if previous is None:
            app.dependency_overrides.pop(connections_router.get_db, None)
        else:
            app.dependency_overrides[connections_router.get_db] = previous


def test_saved_connection_returns_sample_rows_for_a_table(client, db, auth_headers, monkeypatch):
    app = client.app
    previous = app.dependency_overrides.get(connections_router.get_db)
    app.dependency_overrides[connections_router.get_db] = _override_connections_db(db)
    try:
        connection = Connection(
            id="resource-sample-connection",
            name="runtime postgres",
            kind="postgres",
            config={"_encrypted": "ignored-by-mock"},
            status="inactive",
        )
        db.add(connection)
        db.commit()

        fake = MagicMock()
        fake.pull_sample.return_value = [{"id": 1, "customer": "Alice"}]
        monkeypatch.setattr(connections_router, "_load_connection_connector", lambda *args, **kwargs: fake)
        response = client.get(
            f"/api/v2/connections/{connection.id}/resources/codex_pipeline_probe/sample?limit=1",
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["rows"] == [{"id": 1, "customer": "Alice"}]
        fake.pull_sample.assert_called_once_with("codex_pipeline_probe", limit=1)
    finally:
        if previous is None:
            app.dependency_overrides.pop(connections_router.get_db, None)
        else:
            app.dependency_overrides[connections_router.get_db] = previous


def test_viewer_cannot_list_or_sample_saved_connection_resources(client, db):
    app = client.app
    previous = app.dependency_overrides.get(connections_router.get_db)
    app.dependency_overrides[connections_router.get_db] = _override_connections_db(db)
    try:
        db.add(Connection(
            id="viewer-resource-connection",
            name="runtime postgres",
            kind="postgres",
            config={"_encrypted": "ignored-by-authz-test"},
            status="active",
        ))
        viewer = User(
            id="resource-viewer",
            username="resource-viewer",
            email="resource-viewer@test.com",
            password_hash=hash_password("viewer123"),
            role="viewer",
        )
        db.add(viewer)
        db.commit()
        headers = {
            "Authorization": f"Bearer {create_access_token({'sub': viewer.id, 'role': viewer.role})}",
        }

        listed = client.get(
            "/api/v2/connections/viewer-resource-connection/resources",
            headers=headers,
        )
        sampled = client.get(
            "/api/v2/connections/viewer-resource-connection/resources/orders/sample",
            headers=headers,
        )

        assert listed.status_code == 403
        assert sampled.status_code == 403
    finally:
        if previous is None:
            app.dependency_overrides.pop(connections_router.get_db, None)
        else:
            app.dependency_overrides[connections_router.get_db] = previous
