"""Cognito JWT verification, user creation on first request, and the admin check."""

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.config import settings
from api.deps import Tx
from core.enums import Role
from core.errors import AppError, ErrorCode
from db.models import User


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    role: Role
    learn_enabled: bool

    @property
    def is_admin(self) -> bool:
        return self.role == Role.admin


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{settings().cognito_issuer}/.well-known/jwks.json", cache_keys=True)


def _verify_cognito(token: str) -> tuple[str, str]:
    s = settings()
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token, key, algorithms=["RS256"], issuer=s.cognito_issuer, options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as e:
        raise AppError(ErrorCode.unauthenticated, "Please sign in again.") from e
    if claims.get("token_use") == "id":
        if claims.get("aud") != s.cognito_app_client_id:
            raise AppError(ErrorCode.unauthenticated, "Please sign in again.")
    elif claims.get("client_id") != s.cognito_app_client_id:
        raise AppError(ErrorCode.unauthenticated, "Please sign in again.")
    return str(claims["sub"]), str(claims.get("email") or claims.get("username") or "")


def _identity(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AppError(ErrorCode.unauthenticated, "Please sign in.")
    token = header[7:].strip()
    if settings().auth_mode == "dev":
        if not token.startswith("dev:") or "@" not in token:
            raise AppError(ErrorCode.unauthenticated, "Use a token like dev:you@example.com locally.")
        email = token[4:]
        return f"dev:{email}", email
    return _verify_cognito(token)


def current_user(request: Request, tx: Tx) -> CurrentUser:
    sub, email = _identity(request)
    tx.execute(insert(User).values(id=uuid.uuid4(), cognito_sub=sub, email=email).on_conflict_do_nothing())
    user = tx.execute(select(User).where(User.cognito_sub == sub)).scalar_one()
    return CurrentUser(user.id, user.email, Role(user.role), user.learn_enabled)


Me = Annotated[CurrentUser, Depends(current_user)]


def require_admin(user: Me) -> CurrentUser:
    if not user.is_admin:
        raise AppError(ErrorCode.forbidden, "Only admins can open this page.")
    return user


Admin = Annotated[CurrentUser, Depends(require_admin)]
