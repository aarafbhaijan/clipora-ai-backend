"""Ownership checks. Every project query filters by the signed-in user."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.auth import CurrentUser
from core.errors import AppError, ErrorCode
from db.models import Project


def own_project(tx: Session, user: CurrentUser, project_id: uuid.UUID, lock: bool = False) -> Project:
    q = select(Project).where(Project.id == project_id, Project.user_id == user.id, Project.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    project = tx.execute(q).scalar_one_or_none()
    if project is None:
        raise AppError(ErrorCode.not_found, "This video does not exist.")
    return project
