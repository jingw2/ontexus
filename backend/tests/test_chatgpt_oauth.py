import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.config import settings
from app.models.chatgpt_oauth import ChatGptAccountLink, ChatGptLoginTransaction
from app.models.user import User
from app.services.chatgpt_oauth import (
    AUTHORIZE_URL,
    ChatGptLoginError,
    LoginAttempt,
    build_authorization_url,
    exchange_authorization_code,
    pkce_challenge,
    verify_id_token,
)

CLIENT_ID = "oaiapp_test"


@pytest.fixture(autouse=True)
def chatgpt_client(monkeypatch):
    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", "")
    monkeypatch.setattr(settings, "chatgpt_oauth_client_secret", "")
    monkeypatch.setattr(settings, "chatgpt_oauth_redirect_uri", "")


def _b64(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _rsa_jwks():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    numbers = private_key.public_key().public_numbers()
    jwks = {"keys": [{
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }]}
    return pem, jwks


def _id_token(pem, *, nonce, subject="chatgpt-user-1", email="ada@example.com", email_verified=True, audience=CLIENT_ID):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "aud": audience,
            "iss": "https://auth.openai.com",
            "nonce": nonce,
            "email": email,
            "email_verified": email_verified,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_authorization_url_targets_chatgpt_oauth_and_pkce():
    verifier = "a" * 64
    url = build_authorization_url(
        client_id=CLIENT_ID,
        redirect_uri="http://localhost:5173/api/v1/auth/chatgpt/callback",
        state="state-1",
        nonce="nonce-1",
        code_challenge=pkce_challenge(verifier),
    )
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.openai.com/api/accounts/authorize"
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid profile email"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["nonce"] == ["nonce-1"]


def test_exchange_posts_the_chatgpt_token_endpoint(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"id_token": "header.payload.sig"}

    def post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs["data"]
        captured["auth"] = kwargs["auth"]
        return Response()

    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "chatgpt_oauth_client_secret", "secret-value")
    monkeypatch.setattr("app.services.chatgpt_oauth.httpx.post", post)
    attempt = LoginAttempt("verifier", "nonce", "http://localhost:5173/api/v1/auth/chatgpt/callback", "/")
    assert exchange_authorization_code("auth-code", attempt) == "header.payload.sig"
    assert captured["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "auth-code"
    assert captured["data"]["code_verifier"] == "verifier"
    assert captured["data"]["client_id"] == CLIENT_ID
    assert "client_secret" not in captured["data"]
    assert captured["auth"] == (CLIENT_ID, "secret-value")


def test_verify_id_token_rejects_a_nonce_mismatch():
    pem, jwks = _rsa_jwks()
    token = _id_token(pem, nonce="expected")
    with pytest.raises(Exception) as caught:
        verify_id_token(token, nonce="other", client_id=CLIENT_ID, jwks=jwks)
    assert caught.value.code == "failed"


def test_start_requires_a_client_id(client):
    response = client.get("/api/v1/auth/chatgpt/start", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].endswith("/login?chatgpt_error=not_configured")


def test_start_redirects_to_the_chatgpt_authorize_endpoint(client, db, monkeypatch):
    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", CLIENT_ID)
    response = client.get(
        "/api/v1/auth/chatgpt/start",
        params={"return_to": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE_URL
    assert "chatgpt_oauth_tx=" in response.headers["set-cookie"]
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    tx = db.query(ChatGptLoginTransaction).one()
    assert tx.return_to == "/"
    assert parse_qs(location.query)["state"] == [tx.state]


def _complete_login(client, db, monkeypatch, *, subject="chatgpt-user-1", email="ada@example.com", email_verified=True, return_to=None):
    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", CLIENT_ID)
    pem, jwks = _rsa_jwks()
    monkeypatch.setattr("app.services.chatgpt_oauth.fetch_jwks", lambda: jwks)

    def exchange(code, attempt):
        assert code == "auth-code"
        return _id_token(pem, nonce=attempt.nonce, subject=subject, email=email, email_verified=email_verified)

    monkeypatch.setattr("app.routers.auth.exchange_authorization_code", exchange)
    params = {"return_to": return_to} if return_to else None
    start = client.get("/api/v1/auth/chatgpt/start", params=params, follow_redirects=False)
    assert start.status_code == 302
    tx = db.query(ChatGptLoginTransaction).one()
    return client.get(
        "/api/v1/auth/chatgpt/callback",
        params={"code": "auth-code", "state": tx.state},
        cookies={"chatgpt_oauth_tx": tx.id},
        follow_redirects=False,
    )


def test_callback_creates_a_user_and_ontexus_session(client, db, monkeypatch):
    response = _complete_login(client, db, monkeypatch, return_to="/ontologies")
    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:5173/ontologies"
    assert "ontexus_refresh=" in response.headers["set-cookie"]
    user = db.query(User).filter(User.email == "ada@example.com").one()
    assert user.role == "viewer"
    link = db.query(ChatGptAccountLink).one()
    assert link.user_id == user.id
    assert link.subject == "chatgpt-user-1"
    assert db.query(ChatGptLoginTransaction).count() == 0


def test_callback_replay_does_not_sign_in_again(client, db, monkeypatch):
    first = _complete_login(client, db, monkeypatch)
    assert first.status_code == 302
    second = client.get(
        "/api/v1/auth/chatgpt/callback",
        params={"code": "auth-code", "state": "stale"},
        cookies={"chatgpt_oauth_tx": "missing"},
        follow_redirects=False,
    )
    assert second.headers["location"].endswith("/login?chatgpt_error=failed")
    assert db.query(User).filter(User.email == "ada@example.com").count() == 1


def test_callback_logs_in_the_linked_account(client, db, monkeypatch):
    assert _complete_login(client, db, monkeypatch).status_code == 302
    again = _complete_login(client, db, monkeypatch, email="other@example.com")
    assert again.status_code == 302
    assert db.query(User).count() == 1
    assert db.query(ChatGptAccountLink).count() == 1


def test_callback_does_not_link_an_existing_email(client, db, admin_user, monkeypatch):
    response = _complete_login(client, db, monkeypatch, email="admin@test.com")
    assert response.headers["location"].endswith("/login?chatgpt_error=email_in_use")
    assert db.query(ChatGptAccountLink).count() == 0
    assert db.query(User).count() == 1


def test_unverified_email_does_not_claim_an_existing_account(client, db, admin_user, monkeypatch):
    response = _complete_login(
        client, db, monkeypatch, email="admin@test.com", email_verified=False,
    )
    assert response.status_code == 302
    assert "chatgpt_error" not in response.headers["location"]
    created = db.query(User).filter(User.email != "admin@test.com").one()
    assert created.email.endswith("@users.ontexus.local")
    assert db.query(ChatGptAccountLink).one().email is None


def test_denied_callback_does_not_exchange_a_code(client, db, monkeypatch):
    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", CLIENT_ID)
    called = False

    def exchange(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("code was exchanged")

    monkeypatch.setattr("app.routers.auth.exchange_authorization_code", exchange)
    start = client.get("/api/v1/auth/chatgpt/start", follow_redirects=False)
    assert start.status_code == 302
    tx = db.query(ChatGptLoginTransaction).one()
    response = client.get(
        "/api/v1/auth/chatgpt/callback",
        params={"error": "access_denied", "state": tx.state},
        cookies={"chatgpt_oauth_tx": tx.id},
        follow_redirects=False,
    )
    assert called is False
    assert response.headers["location"].endswith("/login?chatgpt_error=denied")
    assert db.query(ChatGptLoginTransaction).count() == 0


@pytest.mark.parametrize("missing", ["sub", "iss", "aud", "exp", "iat", "nonce"])
def test_id_token_requires_identity_claims(missing):
    pem, jwks = _rsa_jwks()
    claims = jwt.get_unverified_claims(_id_token(pem, nonce="expected"))
    del claims[missing]
    token = jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(ChatGptLoginError, match="failed"):
        verify_id_token(token, nonce="expected", client_id=CLIENT_ID, jwks=jwks)


@pytest.mark.parametrize("claim,value", [
    ("aud", "another-client"),
    ("iss", "https://another-issuer.example"),
    ("exp", 1),
])
def test_id_token_rejects_invalid_identity_claims(claim, value):
    pem, jwks = _rsa_jwks()
    claims = jwt.get_unverified_claims(_id_token(pem, nonce="expected"))
    claims[claim] = value
    token = jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(ChatGptLoginError, match="failed"):
        verify_id_token(token, nonce="expected", client_id=CLIENT_ID, jwks=jwks)


@pytest.mark.parametrize("stage,failure", [
    ("token", "timeout"), ("token", "status"), ("token", "json"),
    ("jwks", "timeout"), ("jwks", "status"), ("jwks", "json"),
    ("jwks", "shape"),
])
def test_provider_failures_redirect_without_creating_session(client, db, monkeypatch, stage, failure):
    monkeypatch.setattr(settings, "chatgpt_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr("app.services.chatgpt_oauth._jwks_cache", None)
    client.get("/api/v1/auth/chatgpt/start", follow_redirects=False)
    tx = db.query(ChatGptLoginTransaction).one()
    tx_id, state = tx.id, tx.state
    pem, _ = _rsa_jwks()
    token = _id_token(pem, nonce=tx.nonce)

    def provider_response(url, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("provider unavailable")
        request = httpx.Request("GET", url)
        if failure == "status":
            return httpx.Response(503, request=request)
        if failure == "json":
            return httpx.Response(200, text="not json", request=request)
        return httpx.Response(200, json={"keys": "invalid"}, request=request)

    if stage == "token":
        monkeypatch.setattr("app.services.chatgpt_oauth.httpx.post", provider_response)
    else:
        monkeypatch.setattr("app.routers.auth.exchange_authorization_code", lambda *_: token)
        monkeypatch.setattr("app.services.chatgpt_oauth.httpx.get", provider_response)
    response = client.get(
        "/api/v1/auth/chatgpt/callback",
        params={"code": "auth-code", "state": state},
        cookies={"chatgpt_oauth_tx": tx_id},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].endswith("/login?chatgpt_error=failed")
    assert "ontexus_refresh=" not in response.headers.get("set-cookie", "")
    assert "chatgpt_oauth_tx=" in response.headers["set-cookie"]
    assert db.query(User).count() == 0
    assert db.query(ChatGptLoginTransaction).count() == 0
