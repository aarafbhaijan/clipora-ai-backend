"""Upload limits per user (architecture.md section 16): effective limits, current use, and the checks that
guard a new upload. Errors carry {key, limit, current} so the UI can explain them."""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from core.errors import AppError, ErrorCode
from core.settings import LimitKey, effective_limits
from db.models import AppSetting, Project, UserLimit

MB = 1024 * 1024


def limits_for(tx: Session, user_id: uuid.UUID) -> dict[LimitKey, int]:
    global_values = {r.key: r.value for r in tx.execute(select(AppSetting)).scalars()}
    overrides = {r.key: r.value for r in tx.execute(select(UserLimit).where(UserLimit.user_id == user_id)).scalars()}
    return effective_limits(global_values, overrides)


def usage_for(tx: Session, user_id: uuid.UUID) -> tuple[int, int]:
    """(active videos, storage bytes) over projects that are not deleted."""
    count, size = tx.execute(
        select(func.count(Project.id), func.coalesce(func.sum(Project.size_bytes), 0)).where(
            Project.user_id == user_id, Project.deleted_at.is_(None)
        )
    ).one()
    return int(count), int(size)


def lock_user(tx: Session, user_id: uuid.UUID) -> None:
    """Serialise limit checks for one user until the transaction ends."""
    tx.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:u, 1))"), {"u": str(user_id)})


def _limit_error(key: LimitKey, limit: int, current: float, message: str, code: ErrorCode) -> AppError:
    return AppError(code, message, {"key": key.value, "limit": limit, "current": current})


def file_too_large(limit_mb: int, size_bytes: int) -> AppError:
    return _limit_error(
        LimitKey.max_file_size_mb,
        limit_mb,
        round(size_bytes / MB, 1),
        f"This file is larger than {limit_mb} MB.",
        ErrorCode.file_too_large,
    )


def check_new_upload(tx: Session, user_id: uuid.UUID, size_bytes: int, duration_s: float | None) -> None:
    """Raise if a new video would break any of the user's limits. Locks the user so two uploads can't both pass."""
    lock_user(tx, user_id)
    limits = limits_for(tx, user_id)
    count, used = usage_for(tx, user_id)
    max_videos = limits[LimitKey.max_active_videos]
    max_file = limits[LimitKey.max_file_size_mb]
    max_storage = limits[LimitKey.max_storage_mb]
    max_duration = limits[LimitKey.max_video_duration_s]
    if count >= max_videos:
        raise _limit_error(
            LimitKey.max_active_videos,
            max_videos,
            count,
            f"You have {count} videos. Delete one to upload another.",
            ErrorCode.quota_exceeded,
        )
    if size_bytes > max_file * MB:
        raise file_too_large(max_file, size_bytes)
    if used + size_bytes > max_storage * MB:
        raise _limit_error(
            LimitKey.max_storage_mb,
            max_storage,
            round((used + size_bytes) / MB, 1),
            "This upload would go over your storage limit. Delete a video to make room.",
            ErrorCode.quota_exceeded,
        )
    if duration_s is not None and duration_s > max_duration:
        raise _limit_error(
            LimitKey.max_video_duration_s,
            max_duration,
            duration_s,
            f"This video is longer than {max_duration // 60} minutes.",
            ErrorCode.duration_too_long,
        )
