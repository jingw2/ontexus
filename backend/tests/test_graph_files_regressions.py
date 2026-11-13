import io
import os

import pytest

from app.config import settings


def test_create_relation_rejects_missing_entity_key_without_500(client, auth_headers, ontology):
    response = client.post(
        f"/api/v1/ontologies/{ontology['id']}/graph/relations",
        json={"source_entity": "missing-target"},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_create_relation_rejects_entity_from_another_ontology(client, auth_headers, ontology):
    first = client.post(
        f"/api/v1/ontologies/{ontology['id']}/entities",
        json={"name_cn": "First"},
        headers=auth_headers,
    ).json()["data"]
    other_ontology = client.post(
        "/api/v1/ontologies",
        json={"name": "Other ontology", "domain": "供应链"},
        headers=auth_headers,
    ).json()["data"]
    second = client.post(
        f"/api/v1/ontologies/{other_ontology['id']}/entities",
        json={"name_cn": "Second"},
        headers=auth_headers,
    ).json()["data"]

    response = client.post(
        f"/api/v1/ontologies/{ontology['id']}/graph/relations",
        json={"source_entity": first["id"], "target_entity": second["id"]},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_upload_conversion_failure_removes_partial_file(client, auth_headers, ontology, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))

    class FailedConversion:
        ok = False
        content = None
        error = "conversion failed"

    monkeypatch.setattr("app.routers.files.convert_document", lambda *_args: FailedConversion())
    response = client.post(
        f"/api/v1/ontologies/{ontology['id']}/files",
        files={"file": ("bad.txt", io.BytesIO(b"not usable"), "text/plain")},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert list(tmp_path.rglob("*")) == [tmp_path / ontology["id"]]


def test_delete_file_does_not_remove_disk_file_before_commit(
    client, auth_headers, ontology, admin_user, db, tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    uploaded = client.post(
        f"/api/v1/ontologies/{ontology['id']}/files",
        files={"file": ("ok.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=auth_headers,
    )
    assert uploaded.status_code == 201
    # The public response intentionally omits file_path; discover it from the
    # temporary ontology directory instead.
    paths = [path for path in (tmp_path / ontology["id"]).iterdir() if path.is_file()]
    assert len(paths) == 1
    file_path = paths[0]

    from app.routers import files as files_router

    original_commit = type(db).commit

    def failing_commit(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(type(db), "commit", failing_commit)
    try:
        with pytest.raises(RuntimeError):
            files_router.delete_file(
                ontology["id"], uploaded.json()["data"]["id"], db, current_user=admin_user,
            )
    finally:
        monkeypatch.setattr(type(db), "commit", original_commit)
    assert os.path.exists(file_path)
