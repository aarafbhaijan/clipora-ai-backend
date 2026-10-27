"""Looking up a project's active background job."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.enums import ACTIVE_JOB_STATUSES
from db.models import Job


def active_job(tx: Session, project_id: uuid.UUID, kind: str) -> Job | None:
    """The queued, running or waiting job of this kind, locked for the rest of the transaction."""
    return tx.execute(
        select(Job)
        .where(Job.project_id == project_id, Job.kind == kind, Job.status.in_(ACTIVE_JOB_STATUSES))
        .with_for_update()
    ).scalar_one_or_none()
