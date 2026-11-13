"""Builder pipeline connector safety contracts."""

import uuid

import pytest

from app.models.v2.pipeline import Pipeline


@pytest.fixture
def pipelines_client(client, db):
    """Use the test session for the v2 pipeline router."""
    from app.main import app
    from app.routers.v2 import pipelines

    def _override():
        yield db

    app.dependency_overrides[pipelines.get_db] = _override
    yield client
    app.dependency_overrides.pop(pipelines.get_db, None)


def _headers(client, username="admin", password="admin123"):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _inline_definition(source_type: str) -> dict:
    return {
        "nodes": [{
            "id": "source",
            "type": "connector",
            "config": {
                "source_type": source_type,
                "config_values": {
                    "uri": "mongodb://user:secret@example.invalid/orders"
                    if source_type == "mongodb"
                    else "https://example.invalid/orders",
                    "headers": '{"Authorization":"Bearer secret"}',
                },
            },
        }],
        "edges": [],
    }


@pytest.mark.parametrize("source_type", ["mongodb", "rest_api"])
def test_create_rejects_unsupported_inline_connectors_before_persistence(
    pipelines_client, db, admin_user, source_type,
):
    name = f"unsupported-{source_type}-{uuid.uuid4().hex}"

    response = pipelines_client.post(
        "/api/v2/pipelines",
        json={"name": name, "definition": _inline_definition(source_type)},
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "UNSUPPORTED_INLINE_CONNECTOR"
    assert db.query(Pipeline).filter(Pipeline.name == name).count() == 0


def test_update_rejects_unsupported_inline_connector_before_mutating_definition(
    pipelines_client, db, admin_user,
):
    pipeline = Pipeline(
        name=f"existing-{uuid.uuid4().hex}",
        route="A",
        spec={},
        definition={"nodes": [], "edges": []},
    )
    db.add(pipeline)
    db.commit()
    pipeline_id = pipeline.id

    response = pipelines_client.put(
        f"/api/v2/pipelines/{pipeline_id}",
        json={"definition": _inline_definition("rest_api")},
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "UNSUPPORTED_INLINE_CONNECTOR"
    assert db.get(Pipeline, pipeline_id).definition == {"nodes": [], "edges": []}


def test_validate_marks_legacy_unsupported_connector_definition_invalid(
    pipelines_client, db, admin_user,
):
    pipeline = Pipeline(
        name=f"legacy-{uuid.uuid4().hex}",
        route="A",
        spec={},
        definition=_inline_definition("mongodb"),
    )
    db.add(pipeline)
    db.commit()

    response = pipelines_client.post(
        f"/api/v2/pipelines/{pipeline.id}/validate",
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert payload["errors"] == [{
        "node_id": "source",
        "severity": "error",
        "code": "UNSUPPORTED_INLINE_CONNECTOR",
        "message": "MongoDB/REST inline connector nodes are unsupported in Builder; use File Upload in Builder and manage external connections in Data Connections.",
    }]


def test_legacy_inline_connector_secrets_are_not_returned_by_pipeline_detail(
    pipelines_client, db, admin_user,
):
    pipeline = Pipeline(
        name=f"legacy-secret-{uuid.uuid4().hex}",
        route="A",
        spec={},
        definition=_inline_definition("rest_api"),
    )
    db.add(pipeline)
    db.commit()

    response = pipelines_client.get(
        f"/api/v2/pipelines/{pipeline.id}",
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 200
    assert response.json()["definition"]["nodes"][0]["config"] == {"source_type": "rest_api"}
    assert "Bearer secret" not in response.text


def test_legacy_inline_connector_cannot_queue_a_run(pipelines_client, db, admin_user):
    pipeline = Pipeline(
        name=f"legacy-run-{uuid.uuid4().hex}",
        route="A",
        spec={},
        definition=_inline_definition("mongodb"),
        status="draft",
    )
    db.add(pipeline)
    db.commit()

    response = pipelines_client.post(
        f"/api/v2/pipelines/{pipeline.id}/run",
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "UNSUPPORTED_INLINE_CONNECTOR"
    db.refresh(pipeline)
    assert pipeline.status == "draft"


def test_saved_sql_source_does_not_store_legacy_inline_credentials(
    pipelines_client, db, admin_user,
):
    definition = {
        "nodes": [{
            "id": "sql-source", "type": "connector",
            "config": {
                "source_type": "postgresql",
                "connection_id": "saved-connection",
                "resource": "orders",
                "config_values": {
                    "uri": "postgresql://user:secret@example.invalid/orders",
                    "headers": {"Authorization": "Bearer secret"},
                },
            },
        }],
        "edges": [],
    }
    response = pipelines_client.post(
        "/api/v2/pipelines",
        json={"name": f"safe-sql-{uuid.uuid4().hex}", "definition": definition},
        headers=_headers(pipelines_client),
    )

    assert response.status_code == 201
    stored = db.get(Pipeline, response.json()["id"]).definition
    assert stored["nodes"][0]["config"] == {
        "source_type": "postgresql",
        "connection_id": "saved-connection",
        "resource": "orders",
    }
    assert "secret" not in response.text
