"""Project grants must protect legacy and classic-v2 ontology API surfaces."""

import pytest

from app.main import app
from app.routers.v2 import graph as graph_v2
from app.routers.v2 import logic_actions as logic_actions_v2
from app.routers.v2 import search as search_v2


@pytest.mark.parametrize("path", [
    "/api/v1/ontologies/{id}",
    "/api/v1/ontologies/{id}/export?format=json",
    "/api/v1/ontologies/{id}/entities",
    "/api/v1/ontologies/{id}/logic",
    "/api/v1/ontologies/{id}/actions",
    "/api/v1/ontologies/{id}/graph",
    "/api/v1/ontologies/{id}/files",
    "/api/v2/ontologies/{id}/graph",
    "/api/v2/ontologies/{id}/search/keyword?q=test",
    "/api/v2/ontologies/{id}/logic",
    "/api/v2/ontologies/{id}/state-machines",
    "/api/v2/ontologies/{id}/actions",
])
def test_ungranted_editor_cannot_read_ontology_surfaces(client, db, ontology, editor_user, path):
    def test_db():
        yield db

    for dependency in (graph_v2.get_db, search_v2.get_db, logic_actions_v2.get_db):
        app.dependency_overrides[dependency] = test_db

    token = client.post(
        "/api/v1/auth/login",
        json={"username": editor_user.username, "password": "editor123"},
    ).json()["data"]["access_token"]
    response = client.get(
        path.format(id=ontology["id"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("path", [
    "/api/v1/ontologies/{id}/export?format=json",
    "/api/v2/ontologies/{id}/search/keyword?q=test",
    "/api/v2/ontologies/{id}/logic",
])
def test_creator_grant_keeps_read_routes_available(client, db, ontology, auth_headers, path):
    def test_db():
        yield db

    for dependency in (search_v2.get_db, logic_actions_v2.get_db):
        app.dependency_overrides[dependency] = test_db

    response = client.get(path.format(id=ontology["id"]), headers=auth_headers)
    assert response.status_code == 200


def _editor_headers(client):
    token = client.post(
        "/api/v1/auth/login",
        json={"username": "editor", "password": "editor123"},
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_ungranted_editor_cannot_write_legacy_entities(client, ontology, editor_user):
    response = client.post(
        f"/api/v1/ontologies/{ontology['id']}/entities",
        json={"name_cn": "越权实体", "name_en": "Intruder"},
        headers=_editor_headers(client),
    )
    assert response.status_code == 404


def test_overview_hides_ungranted_ontologies(client, ontology, editor_user):
    response = client.get("/api/v1/overview/stats", headers=_editor_headers(client))
    assert response.status_code == 200
    data = response.json()["data"]
    assert ontology["id"] not in [item["id"] for item in data["recent_ontologies"]]
