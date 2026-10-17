from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agentsphere.models import AuthSession, User, Workspace
from agentsphere.rate_limits import auth_rate_limit
from agentsphere.schemas.auth import (
    CsrfRead,
    Login,
    LoginRead,
    Registration,
    RegistrationRead,
    UserRead,
    WorkspaceRead,
)
from agentsphere.security import (
    DUMMY_HASH,
    PASSWORD_HASHER,
    CurrentUser,
    DatabaseSession,
    csrf_session,
    find_session,
    hash_password,
    new_session,
    now,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
CsrfSession = Annotated[AuthSession, Depends(csrf_session)]
throttled = [Depends(auth_rate_limit)]


@router.get("/csrf", response_model=CsrfRead, dependencies=throttled)
def csrf(request: Request, response: Response, db: DatabaseSession):
    row = find_session(request, db)
    if row is None:
        row = new_session(request, response, db)
    return CsrfRead(csrf_token=row.csrf_token)


@router.post("/register", response_model=RegistrationRead, status_code=201, dependencies=throttled)
def register(data: Registration, request: Request, db: DatabaseSession, session: CsrfSession):
    if session.user_id is not None:
        raise HTTPException(409, "Log out before registering another account.")
    encoded = hash_password(data.password.get_secret_value())
    try:
        # SAVEPOINT allows duplicate email to become 409 instead of a raw SQL error.
        with db.begin_nested():
            user = User(email=data.email, password_hash=encoded, display_name=data.display_name)
            db.add(user)
            db.flush()
            workspace = Workspace(name="Personal workspace", owner_user_id=user.id)
            db.add(workspace)
            db.flush()
    except IntegrityError:
        raise HTTPException(409, "Account could not be registered.") from None
    return RegistrationRead(
        user=UserRead.model_validate(user),
        workspace=WorkspaceRead(
            id=workspace.id,
            name=workspace.name,
            owner_user_id=user.id,
            created_at=workspace.created_at,
            role="owner",
        ),
    )


@router.post("/login", response_model=LoginRead, dependencies=throttled)
def login(
    data: Login, request: Request, response: Response, db: DatabaseSession, session: CsrfSession
):
    if session.user_id is not None:
        raise HTTPException(409, "Log out before signing in again.")
    user = db.scalar(select(User).where(User.email == data.email).with_for_update(key_share=True))
    encoded = user.password_hash if user else DUMMY_HASH
    valid = verify_password(encoded, data.password.get_secret_value())
    if not valid or user is None or not user.is_active:
        raise HTTPException(401, "Invalid email or password.")
    db.refresh(session, with_for_update=True)
    if session.revoked_at is not None or session.expires_at <= now(request):
        raise HTTPException(403, "Obtain a fresh CSRF token.")
    if PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password.get_secret_value())
    session.revoked_at = now(request)
    authenticated = new_session(request, response, db, user.id)
    return LoginRead(
        user=UserRead.model_validate(user),
        csrf_token=authenticated.csrf_token,
        expires_at=authenticated.expires_at,
    )


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser):
    return UserRead.model_validate(user)


def clear_cookie(request, response):
    settings = request.app.state.settings
    response.delete_cookie(
        settings.cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=not settings.allow_insecure_local_http,
    )


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, user: CurrentUser):
    request.state.auth_session.revoked_at = now(request)
    clear_cookie(request, response)


@router.post("/logout-all", status_code=204)
def logout_all(request: Request, response: Response, user: CurrentUser, db: DatabaseSession):
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now(request))
    )
    clear_cookie(request, response)
