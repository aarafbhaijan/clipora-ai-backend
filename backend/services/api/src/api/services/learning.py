"""Learning signals (architecture.md 11.4): feedback events, and the distill job that turns them into memories."""

import contextlib
import uuid

from sqlalchemy.orm import Session

from api.auth import CurrentUser
from core.edl import EDL
from core.enums import Author, JobKind
from core.jobs import DistillPayload
from core.timemap import output_duration
from db.ids import uuid7
from db.models import EdlVersion, FeedbackEvent, Project
from jobqueue import JobBusy, enqueue


def enqueue_distill(tx: Session, user_id: uuid.UUID) -> None:
    # One distill per user at a time; the active one will see this event.
    with contextlib.suppress(JobBusy):
        enqueue(tx, JobKind.distill, DistillPayload(user_id=user_id).model_dump(mode="json"), user_id, None)


def plan_diff(before: EDL, after: EDL) -> dict[str, object]:
    """What the user changed in a plan, as recorded on the feedback event."""
    b_ids = {s.id: s for s in before.segments}
    a_ids = {s.id: s for s in after.segments}
    return {
        "removed": [i for i in b_ids if i not in a_ids],
        "added": [i for i in a_ids if i not in b_ids],
        "trimmed": [
            i
            for i in a_ids
            if i in b_ids and (a_ids[i].src_start, a_ids[i].src_end) != (b_ids[i].src_start, b_ids[i].src_end)
        ],
        "reordered": [s.id for s in before.segments if s.id in a_ids]
        != [s.id for s in after.segments if s.id in b_ids],
        "aspect": [before.output.aspect, after.output.aspect],
        "duration_s": [round(output_duration(before), 1), round(output_duration(after), 1)],
    }


def record_plan_edit(tx: Session, user: CurrentUser, project: Project, old: EdlVersion, new: EDL) -> None:
    """A user edit on top of an agent version is a learning signal. Nothing is recorded when learning is off."""
    if not user.learn_enabled or old.author != Author.agent:
        return
    tx.add(
        FeedbackEvent(
            id=uuid7(),
            user_id=user.id,
            project_id=project.id,
            target_kind="plan",
            target_ref=str(old.version),
            signal="edit",
            diff=plan_diff(EDL.model_validate(old.edl), new),
        )
    )
    enqueue_distill(tx, user.id)
