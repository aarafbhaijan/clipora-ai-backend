"""Export: render one EDL version with FFmpeg (architecture.md section 13).

Pass 1 prepares (load and validate the EDL, download the source, check disk). Pass 2 renders each
segment, pass 3 joins, burns captions and encodes. Progress weights: segments 70, final 25, upload 5.
A failure deletes partial local and S3 files; the exports row becomes failed only when no retry is left.
"""

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

import jobqueue
import storage
from core.edl import EDL
from core.enums import Author, ExportStatus, JobKind
from core.errors import ErrorCode
from core.jobs import ExportPayload
from core.transcript import Transcript
from core.validation import find_issues
from db.models import EdlVersion, Export, Job, Project
from db.session import session_scope
from media.captions import ass, output_cues, srt
from media.export import ExportPlan, plan
from media.ff import MediaError, run_ffmpeg
from worker.context import Cancelled, JobContext, JobFailed, Outcome
from worker.handlers import register

WORK_DIR = os.environ.get("WORK_DIR") or None
W_SEGMENTS, W_FINAL, W_UPLOAD = 70, 25, 5


def _transcript_key(project_id: object) -> str | None:
    with session_scope() as tx:
        job = (
            tx.execute(
                select(Job).where(Job.project_id == project_id, Job.kind == "ingest").order_by(Job.created_at.desc())
            )
            .scalars()
            .first()
        )
    marker = (job.stage_markers or {}).get("transcribe") if job else None
    return str(marker["transcript_key"]) if isinstance(marker, dict) and marker.get("transcript_key") else None


def _set_status(ctx: JobContext, export_id: object, status: ExportStatus, **fields: object) -> None:
    with session_scope() as tx:
        jobqueue.renew(tx, ctx.job_id, ctx.worker)
        e = tx.get(Export, export_id, with_for_update=True)
        if e is not None:
            e.status = status.value
            for k, v in fields.items():
                setattr(e, k, v)


class Render:
    def __init__(self, ctx: JobContext, work: Path) -> None:
        self.ctx = ctx
        self.work = work
        self.payload = ExportPayload.model_validate(ctx.payload)
        self.uploaded: list[str] = []
        with session_scope() as tx:
            e = tx.get(Export, self.payload.export_id)
            p = tx.get(Project, self.payload.project_id)
            row = tx.execute(
                select(EdlVersion).where(
                    EdlVersion.project_id == self.payload.project_id, EdlVersion.version == self.payload.edl_version
                )
            ).scalar_one_or_none()
            job = tx.get(Job, ctx.job_id)
            if e is None or p is None or row is None or p.user_id != ctx.user_id:
                raise JobFailed(ErrorCode.not_found, "export, project or plan version not found")
            self.already_done = e.status == ExportStatus.done
            self.source_key = p.source_key
            self.size_bytes = p.size_bytes
            self.edl = EDL.model_validate(row.edl)
            self.author = Author(row.author)
            self.max_attempts = job.max_attempts if job else 1

    def progress(self, stage: str, pct: float) -> None:
        self.ctx.progress(stage, min(100, int(pct)))

    def run(self) -> Outcome:
        if self.already_done:
            return Outcome.done
        _set_status(self.ctx, self.payload.export_id, ExportStatus.rendering)
        try:
            self._render()
        except (Cancelled, jobqueue.LeaseLost):
            self._cleanup(write_status=False)
            raise
        except JobFailed as e:
            self._cleanup(write_status=True, final=not e.retryable or self.ctx.attempts >= self.max_attempts)
            raise
        except Exception:
            self._cleanup(write_status=True, final=self.ctx.attempts >= self.max_attempts)
            raise
        return Outcome.done

    def _cleanup(self, write_status: bool, final: bool = False) -> None:
        for key in self.uploaded:
            # Best effort: the S3 lifecycle rule catches anything left behind.
            with contextlib.suppress(Exception):
                storage.delete_prefix(key)
        shutil.rmtree(self.work, ignore_errors=True)
        if write_status:
            try:
                status = ExportStatus.failed if final else ExportStatus.queued
                _set_status(self.ctx, self.payload.export_id, status)
            except jobqueue.LeaseLost:
                pass

    def _render(self) -> None:
        # Pass 1: prepare
        issues = find_issues(self.edl, self.author)
        if issues:
            detail = "; ".join(f"{i.path}: {i.message}" for i in issues)[:1000]
            raise JobFailed(ErrorCode.edl_invalid, detail)
        self.work.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.work).free
        if free < 2 * self.size_bytes:
            raise JobFailed(ErrorCode.internal, f"not enough disk: {free} free for {self.size_bytes} source", True)
        self.progress("prepare", 0)
        source = self.work / ("source" + Path(self.source_key).suffix)
        storage.download_file(self.source_key, str(source))
        self.ctx.check()

        transcript = Transcript(cues=[])
        key = _transcript_key(self.payload.project_id)
        if key:
            transcript = Transcript.model_validate_json(storage.get_bytes(key))
        cues = output_cues(self.edl, transcript)
        burn = self.edl.captions.enabled and bool(cues)
        (self.work / "captions.ass").write_text(ass(cues, self.edl.output.width, self.edl.output.height))
        (self.work / "captions.srt").write_text(srt(cues))

        export_plan = plan(self.edl, source.name, "out.mp4", burn)
        self._segments(export_plan)
        self._final(export_plan)
        source.unlink(missing_ok=True)
        self._upload()

    def _segments(self, p: ExportPlan) -> None:
        total = sum(s.length for s in p.segments) or 1.0
        done = 0.0
        for step in p.segments:
            self.ctx.check()
            if step.cmd_name and step.cmd_text:
                (self.work / step.cmd_name).write_text(step.cmd_text)
            base = done

            def on_time(t: float, base: float = base, length: float = step.length) -> None:
                self.progress("segments", W_SEGMENTS * (base + min(t, length)) / total)

            self._ffmpeg(step.args, on_time)
            done += step.length

    def _final(self, p: ExportPlan) -> None:
        self.ctx.check()
        if p.concat_list is not None:
            (self.work / "segments.txt").write_text(p.concat_list)
        duration = p.output_duration or 1.0
        self._ffmpeg(p.final_args, lambda t: self.progress("finish", W_SEGMENTS + W_FINAL * min(1.0, t / duration)))

    def _ffmpeg(self, args: list[str], on_time: Callable[[float], None]) -> None:
        try:
            run_ffmpeg(args, cwd=self.work, on_time=on_time, register=self.ctx.register)
        except MediaError as e:
            self.ctx.check()  # a kill from cancel shows up as an ffmpeg error; report it as the cancel
            raise JobFailed(ErrorCode.internal, str(e)[:2000], retryable=True) from e

    def _upload(self) -> None:
        self.progress("upload", W_SEGMENTS + W_FINAL)
        video_key = storage.export_key(self.payload.project_id, self.payload.export_id, "mp4")
        srt_key = storage.export_key(self.payload.project_id, self.payload.export_id, "srt")
        self.uploaded = [video_key, srt_key]
        storage.upload_file(str(self.work / "out.mp4"), video_key, "video/mp4")
        storage.upload_file(str(self.work / "captions.srt"), srt_key, "application/x-subrip")
        self.ctx.check()
        _set_status(self.ctx, self.payload.export_id, ExportStatus.done, output_key=video_key, srt_key=srt_key)
        self.uploaded = []  # committed: never clean these up now


@register(JobKind.export)
def handle(ctx: JobContext) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="export-", dir=WORK_DIR) as work:
        return Render(ctx, Path(work) / "render").run()
