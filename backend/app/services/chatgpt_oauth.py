"""Sign in with ChatGPT.

Authorization code + PKCE against the ChatGPT OAuth endpoints. The ID token
is verified, then Ontexus issues its own session. OpenAI access and refresh
tokens are not stored.
"""
import base64
import hashlib
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models.chatgpt_oauth import ChatGptAccountLink, ChatGptLoginTransaction
from app.models.user import User
from app.services.auth_service import get_user_by_id, hash_password

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
SCOPES = "openid profile email"
TX_TTL = timedelta(minutes=10)

_jwks_cache: dict | None = None
_jwks_fetched_at = 0.0


class ChatGptLoginError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LoginAttempt:
    code_verifier: str
    nonce: str
    redirect_uri: str
    return_to: str


def safe_return_to(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:500]


def configured_redirect_uri() -> str:
    configured = settings.chatgpt_oauth_redirect_uri.strip()
    if configured:
        return configured
    base = settings.oauth_frontend_base_url.rstrip("/")
    return f"{base}/api/v1/auth/chatgpt/callback"


def frontend_destination(return_to: str) -> str:
    base = settings.oauth_frontend_base_url.rstrip("/")
    return f"{base}{safe_return_to(return_to)}"


def login_error_destination(code: str) -> str:
    base = settings.oauth_frontend_base_url.rstrip("/")
    return f"{base}/login?chatgpt_error={code}"


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}"


def begin_login(db: Session, return_to: str | None) -> tuple[str, str]:
    client_id = settings.chatgpt_oauth_client_id.strip()
    if not client_id:
        raise ChatGptLoginError("not_configured")
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    redirect_uri = configured_redirect_uri()
    tx_id = str(uuid.uuid4())
    db.add(ChatGptLoginTransaction(
        id=tx_id,
        state=state,
        code_verifier=verifier,
        nonce=nonce,
        redirect_uri=redirect_uri,
        return_to=safe_return_to(return_to),
        expires_at=datetime.now(timezone.utc) + TX_TTL,
    ))
    db.commit()
    url = build_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=pkce_challenge(verifier),
    )
    return tx_id, url


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)


def consume_transaction(db: Session, tx_id: str | None, state: str | None) -> LoginAttempt:
    if not tx_id or not state:
        raise ChatGptLoginError("failed")
    tx = db.get(ChatGptLoginTransaction, tx_id)
    if tx is None:
        raise ChatGptLoginError("failed")
    expired = _as_utc(tx.expires_at) <= datetime.now(timezone.utc)
    state_ok = _same(tx.state, state)
    attempt = LoginAttempt(tx.code_verifier, tx.nonce, tx.redirect_uri, tx.return_to)
    db.delete(tx)
    db.commit()
    if expired or not state_ok:
        raise ChatGptLoginError("failed")
    return attempt


def exchange_authorization_code(code: str, attempt: LoginAttempt) -> str:
    client_id = settings.chatgpt_oauth_client_id.strip()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": attempt.redirect_uri,
        "client_id": client_id,
        "code_verifier": attempt.code_verifier,
    }
    secret = settings.chatgpt_oauth_client_secret.strip()
    auth = (client_id, secret) if secret else None
    try:
        response = httpx.post(
            TOKEN_URL,
            data=data,
            headers={"accept": "application/json"},
            auth=auth,
            timeout=10,
        )
    except httpx.HTTPError:
        logger.warning("chatgpt token exchange failed")
        raise ChatGptLoginError("failed")
    if response.status_code != 200:
        logger.warning("chatgpt token exchange failed status=%s", response.status_code)
        raise ChatGptLoginError("failed")
    try:
        body = response.json()
    except ValueError:
        raise ChatGptLoginError("failed") from None
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise ChatGptLoginError("failed")
    return id_token


def fetch_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache is not None and now - _jwks_fetched_at < 600:
        return _jwks_cache
    try:
        response = httpx.get(JWKS_URL, timeout=10)
        response.raise_for_status()
        keys = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("chatgpt signing key retrieval failed")
        raise ChatGptLoginError("failed") from None
    if (
        not isinstance(keys, dict)
        or not isinstance(keys.get("keys"), list)
        or not keys["keys"]
        or not all(isinstance(key, dict) for key in keys["keys"])
    ):
        raise ChatGptLoginError("failed")
    _jwks_cache = keys
    _jwks_fetched_at = now
    return _jwks_cache


def verify_id_token(id_token: str, *, nonce: str, client_id: str, jwks: dict | None = None) -> dict:
    keys = jwks if jwks is not None else fetch_jwks()
    try:
        claims = jwt.decode(
            id_token,
            keys,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            options={
                "leeway": 5,
                "require_sub": True,
                "require_iss": True,
                "require_aud": True,
                "require_exp": True,
                "require_iat": True,
            },
        )
    except JWTError:
        raise ChatGptLoginError("failed")
    token_nonce = claims.get("nonce")
    subject = claims.get("sub")
    if not isinstance(token_nonce, str) or not _same(token_nonce, nonce):
        raise ChatGptLoginError("failed")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise ChatGptLoginError("failed")
    return claims


def _verified_email(claims: dict) -> str | None:
    email = claims.get("email")
    if claims.get("email_verified") is not True:
        return None
    if not isinstance(email, str) or "@" not in email or len(email) > 200:
        return None
    return email


def _username_for(db: Session, email: str | None, subject: str) -> str:
    base = "chatgpt"
    if email:
        local = re.sub(r"[^A-Za-z0-9_]", "", email.split("@", 1)[0])[:40]
        if local:
            base = local
    else:
        base = "chatgpt_" + hashlib.sha256(subject.encode()).hexdigest()[:8]
    candidate = base[:50]
    suffix = 0
    while db.query(User.id).filter(User.username == candidate).first():
        suffix += 1
        extra = f"_{suffix}"
        candidate = f"{base[:50 - len(extra)]}{extra}"
    return candidate


def _stored_email(claims: dict, subject: str) -> tuple[str, str | None]:
    verified = _verified_email(claims)
    if verified:
        return verified, verified
    digest = hashlib.sha256(subject.encode()).hexdigest()[:32]
    return f"chatgpt-{digest}@users.ontexus.local", None


def resolve_user(db: Session, claims: dict) -> User:
    client_id = settings.chatgpt_oauth_client_id.strip()
    subject = claims["sub"]
    link = db.query(ChatGptAccountLink).filter_by(
        issuer=ISSUER, client_id=client_id, subject=subject,
    ).one_or_none()
    if link is not None:
        user = get_user_by_id(db, link.user_id)
        if user is None or not user.is_active:
            raise ChatGptLoginError("failed")
        return user
    email, link_email = _stored_email(claims, subject)
    if link_email and db.query(User.id).filter(User.email == link_email).first():
        raise ChatGptLoginError("email_in_use")
    user = User(
        id=str(uuid.uuid4()),
        username=_username_for(db, link_email, subject),
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role="viewer",
    )
    db.add(user)
    db.flush()
    db.add(ChatGptAccountLink(
        user_id=user.id,
        issuer=ISSUER,
        client_id=client_id,
        subject=subject,
        email=link_email,
    ))
    db.flush()
    return user
