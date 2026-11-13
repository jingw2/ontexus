"""上传限制 — 扩展名白名单 + 大小上限"""
import io

import pytest
from fastapi import HTTPException

from app.config import settings


@pytest.fixture
def datasets_client(client, db):
    """v2 datasets 路由用模块内 get_db, 需单独覆盖到测试库"""
    from app.main import app
    from app.routers.v2 import datasets

    def _override():
        yield db

    app.dependency_overrides[datasets.get_db] = _override
    yield client
    app.dependency_overrides.pop(datasets.get_db, None)


def _headers(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def test_upload_rejects_disallowed_extension(datasets_client, admin_user):
    r = datasets_client.post(
        "/api/v2/datasets/upload",
        files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        headers=_headers(datasets_client),
    )
    assert r.status_code == 400


def test_upload_rejects_oversized_file(datasets_client, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    big = b"a" * (1024 * 1024 + 1)
    r = datasets_client.post(
        "/api/v2/datasets/upload",
        files={"file": ("big.csv", io.BytesIO(big), "text/csv")},
        headers=_headers(datasets_client),
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_bounded_upload_read_stops_after_limit_plus_one_byte(monkeypatch):
    from app.routers.v2.datasets import _read_upload_bounded

    limit = 8
    payload = b"x" * (limit + 1000)

    class TrackedUpload:
        def __init__(self, value):
            self.value = value
            self.offset = 0
            self.read_sizes = []

        async def read(self, size=-1):
            self.read_sizes.append(size)
            end = min(self.offset + size, len(self.value))
            chunk = self.value[self.offset:end]
            self.offset = end
            return chunk

    upload = TrackedUpload(payload)

    with pytest.raises(HTTPException) as error:
        await _read_upload_bounded(upload, limit, chunk_size=4)

    assert error.value.status_code == 413
    assert upload.offset == limit + 1
    assert upload.read_sizes == [4, 4, 1]


def test_upload_accepts_normal_csv(datasets_client, admin_user):
    r = datasets_client.post(
        "/api/v2/datasets/upload",
        files={"file": ("ok.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        headers=_headers(datasets_client),
    )
    assert r.status_code == 201
