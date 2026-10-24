import secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user
from app.limiter import limiter
from app.schemas.auth import LoginRequest, TokenResponse, RegisterRequest, PasswordChangeRequest
from app.schemas.user import UserOut
from app.services.auth_service import authenticate_user, create_access_token, hash_password, verify_password
from app.services.chatgpt_oauth import (
    ChatGptLoginError,
    begin_login,
    consume_transaction,
    exchange_authorization_code,
    frontend_destination,
    login_error_destination,
    resolve_user,
    verify_id_token,
)
from app.services.auth_refresh import (
    RefreshSessionError,
    create_refresh_session,
    revoke_refresh_families,
    revoke_refresh_session,
    rotate_refresh_session,
)
from app.models.user import User
from app.config import settings
import uuid

router = APIRouter()

REFRESH_COOKIE = "ontexus_refresh"
CSRF_COOKIE = "csrf_token"
CHATGPT_TX_COOKIE = "chatgpt_oauth_tx"


def _set_auth_cookies(response: Response, refresh_token: str) -> None:
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/api/v1/auth",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _validate_csrf(request: Request, csrf_cookie: str | None) -> None:
    header = request.headers.get("X-CSRF-Token")
    origin = request.headers.get("Origin")
    if origin:
        allowed = {value.strip() for value in settings.cors_origins.split(",") if value.strip()}
        if origin not in allowed:
            raise HTTPException(status_code=403, detail="CSRF_INVALID")
    if not csrf_cookie or not header or not secrets.compare_digest(header, csrf_cookie):
        raise HTTPException(status_code=403, detail="CSRF_INVALID")


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token({"sub": user.id, "role": user.role})
    refresh_token = create_refresh_session(db, user.id)
    _set_auth_cookies(response, refresh_token)
    return {"data": {"access_token": token, "token_type": "bearer"}, "message": "ok"}


@router.post("/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    _validate_csrf(request, request.cookies.get(CSRF_COOKIE))
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token required")
    try:
        access_token, successor = rotate_refresh_session(db, refresh_token)
    except RefreshSessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    _set_auth_cookies(response, successor)
    return {"data": {"access_token": access_token, "token_type": "bearer"}, "message": "ok"}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    _validate_csrf(request, request.cookies.get(CSRF_COOKIE))
    if refresh_token:
        revoke_refresh_session(db, refresh_token)
    _clear_auth_cookies(response)
    return {"message": "ok"}


def _chatgpt_error(code: str) -> RedirectResponse:
    response = RedirectResponse(login_error_destination(code), status_code=302)
    response.delete_cookie(CHATGPT_TX_COOKIE, path="/api/v1/auth/chatgpt")
    return response


@router.get("/chatgpt/start")
@limiter.limit("10/minute")
def chatgpt_start(request: Request, return_to: str | None = None, db: Session = Depends(get_db)):
    try:
        tx_id, url = begin_login(db, return_to)
    except ChatGptLoginError as exc:
        return _chatgpt_error(exc.code)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        CHATGPT_TX_COOKIE,
        tx_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,
        path="/api/v1/auth/chatgpt",
    )
    return response


@router.get("/chatgpt/callback")
@limiter.limit("20/minute")
def chatgpt_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    tx_id = request.cookies.get(CHATGPT_TX_COOKIE)
    if error or not code:
        if tx_id and state:
            try:
                consume_transaction(db, tx_id, state)
            except ChatGptLoginError:
                pass
        return _chatgpt_error("denied" if error == "access_denied" else "failed")
    try:
        attempt = consume_transaction(db, tx_id, state)
        id_token = exchange_authorization_code(code, attempt)
        claims = verify_id_token(
            id_token,
            nonce=attempt.nonce,
            client_id=settings.chatgpt_oauth_client_id.strip(),
        )
        user = resolve_user(db, claims)
        refresh_token = create_refresh_session(db, user.id)
    except ChatGptLoginError as exc:
        return _chatgpt_error(exc.code)
    response = RedirectResponse(frontend_destination(attempt.return_to), status_code=302)
    _set_auth_cookies(response, refresh_token)
    response.delete_cookie(CHATGPT_TX_COOKIE, path="/api/v1/auth/chatgpt")
    return response


@router.post("/register", status_code=201)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter((User.username == body.username) | (User.email == body.email)).first():
        raise HTTPException(status_code=409, detail="Username or email already exists")
    user = User(
        id=str(uuid.uuid4()),
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role="viewer",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"data": UserOut.model_validate(user).model_dump(), "message": "ok"}


@router.get("/profile")
def profile(current_user: User = Depends(get_current_user)):
    return {"data": UserOut.model_validate(current_user).model_dump(), "message": "ok"}


@router.put("/password")
def change_password(
    body: PasswordChangeRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    current_user.password_hash = hash_password(body.new_password)
    revoke_refresh_families(db, current_user.id)
    _clear_auth_cookies(response)
    return {"message": "Password updated"}
