"""Curated dataset deletion — MinIO object cleanup"""
import uuid

import pytest

from app.models.v2.dataset import Dataset, DatasetVersion, MediaItem


@pytest.fixture
def curated_client(client, db):
    """v2 curated 路由用模块内 get_db, 需单独覆盖到测试库"""
    from app.main import app
    from app.routers.v2 import curated

    def _override():
        yield db

    app.dependency_overrides[curated.get_db] = _override
    yield client
    app.dependency_overrides.pop(curated.get_db, None)


def _login(client, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _make_curated_with_media(db) -> tuple[str, list[str]]:
    ds = Dataset(id=str(uuid.uuid4()), name=f"del-{uuid.uuid4().hex[:6]}", kind="curated")
    db.add(ds)
    db.flush()
    ver = DatasetVersion(id=str(uuid.uuid4()), dataset_id=ds.id, version_no=1)
    db.add(ver)
    db.flush()
    uris = [f"s3://curated/{uuid.uuid4().hex}.pdf", f"s3://curated/{uuid.uuid4().hex}.docx"]
    media = MediaItem(
        id=str(uuid.uuid4()), dataset_version_id=ver.id, media_type="pdf",
        storage_uri=uris[0], ocr_result_uri=uris[1],
    )
    db.add(media)
    db.commit()
    return ds.id, uris


def test_delete_curated_cleans_up_minio_objects(curated_client, db, admin_user, monkeypatch):
    """Deleting a curated dataset must remove its MediaItem MinIO objects
    (storage_uri and ocr_result_uri) — previously nothing in the delete path
    ever called StorageService.delete_object, leaving them as permanent
    orphaned storage."""
    ds_id, expected_uris = _make_curated_with_media(db)
    deleted = []

    class FakeStorage:
        def delete_object(self, uri):
            deleted.append(uri)

    monkeypatch.setattr("app.services.storage_service.get_storage_service", lambda: FakeStorage())

    headers = _login(curated_client, "admin", "admin123")
    r = curated_client.delete(f"/api/v2/curated/{ds_id}", headers=headers)
    assert r.status_code == 204
    assert sorted(deleted) == sorted(expected_uris)


def test_delete_curated_survives_minio_failure(curated_client, db, admin_user, monkeypatch):
    """MinIO cleanup is best-effort: a Postgres delete that already committed
    must still return 204 even if object storage cleanup blows up."""
    ds_id, _ = _make_curated_with_media(db)

    class BrokenStorage:
        def delete_object(self, uri):
            raise ConnectionError("minio unreachable")

    monkeypatch.setattr("app.services.storage_service.get_storage_service", lambda: BrokenStorage())

    headers = _login(curated_client, "admin", "admin123")
    r = curated_client.delete(f"/api/v2/curated/{ds_id}", headers=headers)
    assert r.status_code == 204
