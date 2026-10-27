"""Admin routes (architecture.md section 16).

The only code allowed to read across users. It reads account fields, limits and usage, never
another user's videos, transcripts, messages, plans or memories.
"""

import base64
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import Row, String, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.auth import Admin
from api.deps import Tx
from api.schemas import (
    AdminSettingsOut,
    AdminSettingsPatchIn,
    AdminUsageOut,
    AdminUserDetail,
    AdminUserPage,
    AdminUserPatchIn,
    AdminUserRow,
    LimitSpecOut,
    TokenTotals,
    UsageGroup,
)
from api.services.quotas import limits_for
from core.enums import Role
from core.errors import AppError, ErrorCode
from core.settings import LIMITS, LimitKey, check_value
from db.models import AppSetting, LlmUsage, Project, User, UserLimit

router = APIRouter(prefix="/admin", tags=["admin"])

PAGE_SIZE = 50


def _range(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """Inclusive date range in UTC. Default: this calendar month so far."""
    today = datetime.now(UTC).date()
    start = start or today.replace(day=1)
    end = end or today
    if end < start:
        raise AppError(ErrorCode.bad_request, "The end date is before the start date.")
    return datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)


_TOTALS = (
    func.coalesce(func.sum(LlmUsage.input_tokens), 0),
    func.coalesce(func.sum(LlmUsage.output_tokens), 0),
    func.coalesce(func.sum(LlmUsage.cache_read_tokens), 0),
    func.coalesce(func.sum(LlmUsage.cache_write_tokens), 0),
    func.count(LlmUsage.id),
    func.sum(LlmUsage.cost_usd),
    func.count(LlmUsage.id).filter(LlmUsage.cost_usd.is_(None)),
)


def _totals(row: Row[Any] | Sequence[Any] | None) -> TokenTotals:
    if row is None:
        return TokenTotals()
    i, o, cr, cw, calls, cost, unpriced = row
    return TokenTotals(
        input_tokens=int(i),
        output_tokens=int(o),
        cache_read_tokens=int(cr),
        cache_write_tokens=int(cw),
        calls=int(calls),
        cost_usd=Decimal(cost) if cost is not None else None,
        unpriced_calls=int(unpriced),
    )


def _limits_out(tx: Session) -> AdminSettingsOut:
    values = {r.key: r.value for r in tx.execute(select(AppSetting)).scalars()}
    return AdminSettingsOut(
        limits=[
            LimitSpecOut(
                key=s.key,
                label=s.label,
                unit=s.unit,
                default=s.default,
                minimum=s.minimum,
                maximum=s.maximum,
                value=int(values.get(s.key.value, s.default)),
            )
            for s in LIMITS.values()
        ]
    )


@router.get("/settings", response_model=AdminSettingsOut)
def get_settings(admin: Admin, tx: Tx) -> AdminSettingsOut:
    return _limits_out(tx)


@router.patch("/settings", response_model=AdminSettingsOut)
def patch_settings(body: AdminSettingsPatchIn, admin: Admin, tx: Tx) -> AdminSettingsOut:
    for key, value in body.values.items():
        check_value(key, value)
    for key, value in body.values.items():
        tx.execute(
            insert(AppSetting)
            .values(key=key, value=value, updated_by=admin.id, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": value, "updated_by": admin.id, "updated_at": func.now()},
            )
        )
    return _limits_out(tx)


def _storage_by_user(tx: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    rows = tx.execute(
        select(Project.user_id, func.count(Project.id), func.coalesce(func.sum(Project.size_bytes), 0))
        .where(Project.user_id.in_(user_ids), Project.deleted_at.is_(None))
        .group_by(Project.user_id)
    ).all()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def _tokens_by_user(tx: Session, user_ids: list[uuid.UUID], lo: datetime, hi: datetime) -> dict[uuid.UUID, TokenTotals]:
    rows = tx.execute(
        select(LlmUsage.user_id, *_TOTALS)
        .where(LlmUsage.user_id.in_(user_ids), LlmUsage.created_at >= lo, LlmUsage.created_at < hi)
        .group_by(LlmUsage.user_id)
    ).all()
    return {r[0]: _totals(r[1:]) for r in rows}


def _row(u: User, storage: dict[uuid.UUID, tuple[int, int]], tokens: dict[uuid.UUID, TokenTotals]) -> AdminUserRow:
    count, size = storage.get(u.id, (0, 0))
    return AdminUserRow(
        id=u.id,
        email=u.email,
        role=Role(u.role),
        created_at=u.created_at,
        active_videos=count,
        storage_bytes=size,
        tokens=tokens.get(u.id, TokenTotals()),
    )


@router.get("/users", response_model=AdminUserPage)
def list_users(
    admin: Admin,
    tx: Tx,
    start: date | None = None,
    end: date | None = None,
    q: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
) -> AdminUserPage:
    lo, hi = _range(start, end)
    stmt = select(User)
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q}%"))
    if cursor:
        try:
            created, uid = base64.urlsafe_b64decode(cursor).decode().split("|")
            stmt = stmt.where(
                (User.created_at < datetime.fromisoformat(created))
                | ((User.created_at == datetime.fromisoformat(created)) & (User.id < uuid.UUID(uid)))
            )
        except ValueError as e:
            raise AppError(ErrorCode.bad_request, "Bad cursor.") from e
    users = list(tx.execute(stmt.order_by(User.created_at.desc(), User.id.desc()).limit(PAGE_SIZE + 1)).scalars())
    page, more = users[:PAGE_SIZE], len(users) > PAGE_SIZE
    ids = [u.id for u in page]
    storage, tokens = _storage_by_user(tx, ids), _tokens_by_user(tx, ids, lo, hi)
    next_cursor = (
        base64.urlsafe_b64encode(f"{page[-1].created_at.isoformat()}|{page[-1].id}".encode()).decode()
        if more and page
        else None
    )
    return AdminUserPage(items=[_row(u, storage, tokens) for u in page], next_cursor=next_cursor)


def _get_user(tx: Session, user_id: uuid.UUID) -> User:
    u = tx.get(User, user_id)
    if u is None:
        raise AppError(ErrorCode.not_found, "That user does not exist.")
    return u


def _grouped(
    tx: Session, column: Any, user_id: uuid.UUID | None, lo: datetime, hi: datetime
) -> list[tuple[str, TokenTotals]]:
    stmt = select(column, *_TOTALS).where(LlmUsage.created_at >= lo, LlmUsage.created_at < hi)
    if user_id is not None:
        stmt = stmt.where(LlmUsage.user_id == user_id)
    rows = tx.execute(stmt.group_by(column).order_by(column)).all()
    return [(str(r[0]) if r[0] is not None else "", _totals(r[1:])) for r in rows]


def _detail(tx: Session, u: User, lo: datetime, hi: datetime) -> AdminUserDetail:
    storage, tokens = _storage_by_user(tx, [u.id]), _tokens_by_user(tx, [u.id], lo, hi)
    by_project = _grouped(tx, LlmUsage.project_id, u.id, lo, hi)
    titles = {
        str(p.id): p.title
        for p in tx.execute(select(Project).where(Project.id.in_([uuid.UUID(k) for k, _ in by_project if k]))).scalars()
    }
    overrides = {
        LimitKey(r.key): int(r.value)
        for r in tx.execute(select(UserLimit).where(UserLimit.user_id == u.id)).scalars()
        if r.key in LimitKey.__members__
    }
    day = func.to_char(func.date_trunc("day", LlmUsage.created_at), "YYYY-MM-DD")
    return AdminUserDetail(
        user=_row(u, storage, tokens),
        limits=limits_for(tx, u.id),
        overrides=overrides,
        by_project=[
            UsageGroup(key=k, label=titles.get(k, "Deleted video" if k else "Learning (no video)"), tokens=t)
            for k, t in by_project
        ],
        by_kind=[UsageGroup(key=k, label=k, tokens=t) for k, t in _grouped(tx, LlmUsage.job_kind, u.id, lo, hi)],
        by_day=[UsageGroup(key=k, label=k, tokens=t) for k, t in _grouped(tx, day, u.id, lo, hi)],
    )


@router.get("/users/{user_id}", response_model=AdminUserDetail)
def get_user(
    user_id: uuid.UUID, admin: Admin, tx: Tx, start: date | None = None, end: date | None = None
) -> AdminUserDetail:
    lo, hi = _range(start, end)
    return _detail(tx, _get_user(tx, user_id), lo, hi)


@router.patch("/users/{user_id}", response_model=AdminUserDetail)
def patch_user(user_id: uuid.UUID, body: AdminUserPatchIn, admin: Admin, tx: Tx) -> AdminUserDetail:
    u = tx.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
    if u is None:
        raise AppError(ErrorCode.not_found, "That user does not exist.")
    if body.role is not None and body.role != u.role:
        if u.role == Role.admin:
            admins = tx.execute(select(User.id).where(User.role == Role.admin.value).with_for_update()).all()
            if len(admins) <= 1:
                raise AppError(ErrorCode.bad_request, "There must be at least one admin.")
        u.role = body.role.value
    for key, value in (body.overrides or {}).items():
        if value is None:
            tx.execute(delete(UserLimit).where(UserLimit.user_id == u.id, UserLimit.key == key))
            continue
        check_value(key, value)
        tx.execute(
            insert(UserLimit)
            .values(user_id=u.id, key=key, value=value, updated_by=admin.id, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[UserLimit.user_id, UserLimit.key],
                set_={"value": value, "updated_by": admin.id, "updated_at": func.now()},
            )
        )
    tx.flush()
    lo, hi = _range(None, None)
    return _detail(tx, u, lo, hi)


@router.get("/usage", response_model=AdminUsageOut)
def usage(
    admin: Admin,
    tx: Tx,
    start: date | None = None,
    end: date | None = None,
    group_by: Literal["user", "day", "job_kind"] = "user",
) -> AdminUsageOut:
    lo, hi = _range(start, end)
    total = _totals(tx.execute(select(*_TOTALS).where(LlmUsage.created_at >= lo, LlmUsage.created_at < hi)).one())
    if group_by == "user":
        rows = _grouped(tx, cast(LlmUsage.user_id, String), None, lo, hi)
        emails = {
            str(u.id): u.email
            for u in tx.execute(select(User).where(User.id.in_([uuid.UUID(k) for k, _ in rows if k]))).scalars()
        }
        groups = [UsageGroup(key=k, label=emails.get(k, k), tokens=t) for k, t in rows]
        groups.sort(key=lambda g: g.tokens.input_tokens + g.tokens.output_tokens, reverse=True)
    elif group_by == "day":
        day = func.to_char(func.date_trunc("day", LlmUsage.created_at), "YYYY-MM-DD")
        groups = [UsageGroup(key=k, label=k, tokens=t) for k, t in _grouped(tx, day, None, lo, hi)]
    else:
        groups = [UsageGroup(key=k, label=k, tokens=t) for k, t in _grouped(tx, LlmUsage.job_kind, None, lo, hi)]
    return AdminUsageOut(total=total, groups=groups)
