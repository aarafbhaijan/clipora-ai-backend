"""Rebuild the editor copy (proxy) of videos ingested when it was 360 px, at today's quality.

    python -m worker.backfill_proxy                 # every ready video whose copy is older
    python -m worker.backfill_proxy <project_id>... # just these (rebuilt even if current)

Runs inline (not through the queue), one video at a time. The new copy gets a new name (`proxy-720p.mp4`), the
ingest job's "proxy" marker points at it, and the old copy is deleted. Open editors pick it up on their next poll.
"""

import argparse
import logging
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from core.enums import ProjectStatus
from db.models import Job, Project
from db.session import session_scope
from media import commands
from media.ff import run_ffmpeg

log = logging.getLogger("backfill")


def _ingest_job(tx: Session, project_id: uuid.UUID) -> Job | None:
    return tx.execute(
        select(Job).where(Job.project_id == project_id, Job.kind == "ingest").order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def _key_name(width: int, height: int) -> str:
    return f"proxy-{min(commands.PROXY_SHORT_SIDE, width, height)}p.mp4"


def _todo(ids: list[str]) -> list[uuid.UUID]:
    with session_scope() as tx:
        q = select(Project).where(Project.status == ProjectStatus.ready.value, Project.deleted_at.is_(None))
        if ids:
            return [p.id for p in tx.execute(q.where(Project.id.in_([uuid.UUID(i) for i in ids]))).scalars()]
        out = []
        for p in tx.execute(q.order_by(Project.created_at)).scalars():
            job = _ingest_job(tx, p.id)
            proxy = (job.stage_markers or {}).get("proxy") if job is not None else None
            current = _key_name(int(p.width or 0), int(p.height or 0))
            if isinstance(proxy, dict) and not str(proxy.get("key", "")).endswith(current):
                out.append(p.id)
        return out


def backfill(project_id: uuid.UUID) -> str:
    with session_scope() as tx:
        p = tx.get(Project, project_id)
        job = _ingest_job(tx, project_id)
        assert p is not None and job is not None
        old = str(((job.stage_markers or {}).get("proxy") or {}).get("key") or "")
        source_key, user_id = p.source_key, p.user_id
        width, height = int(p.width or 0), int(p.height or 0)
    short = min(commands.PROXY_SHORT_SIDE, width, height)
    key: str = storage.asset_key(user_id, project_id, _key_name(width, height))
    with tempfile.TemporaryDirectory(prefix="proxy-") as work:
        src = Path(work) / ("source" + Path(source_key).suffix)
        out = Path(work) / "proxy.mp4"
        storage.download_file(source_key, str(src))
        run_ffmpeg(commands.proxy(str(src), str(out), short))
        storage.upload_file(str(out), key, "video/mp4")
    with session_scope() as tx:
        job = _ingest_job(tx, project_id)
        assert job is not None
        job.stage_markers = {**(job.stage_markers or {}), "proxy": {"key": key}}
    if old and old != key:
        storage.client().delete_object(Bucket=storage.bucket(), Key=old)
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_ids", nargs="*")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    todo = _todo(args.project_ids)
    log.info("%d video(s) to rebuild", len(todo))
    for pid in todo:
        try:
            log.info("%s: %s", pid, backfill(pid))
        except Exception:
            log.exception("%s: failed", pid)


if __name__ == "__main__":
    main()
