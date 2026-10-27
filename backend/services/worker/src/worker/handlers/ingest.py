"""Ingest: 8 stages with a marker after each (architecture.md section 10).

Each stage uploads to a fixed key and records its marker data (the contract is in
backend/services/api/src/api/assets.py), so a repeat run after a lost lease skips finished stages.
When all stages are done, one transaction sets the project ready and enqueues the ideas job.
"""

import math
import os
import shutil
import tempfile
import wave
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select

import jobqueue
import storage
from core.crop import Detections
from core.enums import JobKind, ProjectStatus
from core.errors import ErrorCode
from core.jobs import IngestPayload, SuggestPayload
from core.settings import LimitKey, effective_limits
from core.transcript import Transcript, Word, to_srt
from db.models import AppSetting, Project, UserLimit
from db.session import session_scope
from media import commands
from media.cues import group_words, parse_srt
from media.ff import run_ffmpeg
from media.probe import probe
from media.waveform import waveform
from worker.context import JobContext, JobFailed, Outcome
from worker.handlers import register
from worker.speakers import KEY_NAME as SPEAKERS_KEY_NAME
from worker.speakers import with_turns

WORK_DIR = os.environ.get("WORK_DIR") or None

# Share of the progress bar per stage, in order. Sums to 100.
WEIGHTS: dict[str, int] = {
    "probe": 3,
    "transcribe": 35,
    "proxy": 14,
    "thumbnails": 6,
    "waveform": 4,
    "detect": 20,
    "speakers": 8,
    "index": 10,
}


def _base(stage: str) -> int:
    total = 0
    for name, w in WEIGHTS.items():
        if name == stage:
            return total
        total += w
    raise KeyError(stage)


def _reporter(ctx: JobContext, stage: str) -> Callable[[float], None]:
    """Progress callback for a fraction 0..1 of one stage. Writes at most once per whole percent."""
    last = [-1]

    def report(fraction: float) -> None:
        pct = _base(stage) + int(WEIGHTS[stage] * max(0.0, min(1.0, fraction)))
        if pct != last[0]:
            last[0] = pct
            ctx.progress(stage, pct)

    return report


def _fail_project(ctx: JobContext, project_id: Any, code: ErrorCode, detail: str) -> JobFailed:
    with session_scope() as tx:
        jobqueue.renew(tx, ctx.job_id, ctx.worker)
        p = tx.get(Project, project_id, with_for_update=True)
        if p is not None:
            p.status = ProjectStatus.failed.value
    return JobFailed(code, detail, retryable=False)


def _max_duration(user_id: Any) -> int:
    with session_scope() as tx:
        global_values = {r.key: r.value for r in tx.execute(select(AppSetting)).scalars()}
        overrides = {
            r.key: r.value for r in tx.execute(select(UserLimit).where(UserLimit.user_id == user_id)).scalars()
        }
    return effective_limits(global_values, overrides)[LimitKey.max_video_duration_s]


@lru_cache(maxsize=1)
def _whisper() -> Any:
    from faster_whisper import WhisperModel  # heavy import, worker-ingest only

    return WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")


def load_pcm(path: Path) -> Any:
    """16 kHz mono s16 WAV as float32 samples. Whisper gets an array, so it never decodes with PyAV
    (faster-whisper 1.2.1 passes an argument newer PyAV releases reject)."""
    import numpy as np

    with wave.open(str(path), "rb") as f:
        frames = f.readframes(f.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


class Ingest:
    def __init__(self, ctx: JobContext, work: Path) -> None:
        self.ctx = ctx
        self.work = work
        self.payload = IngestPayload.model_validate(ctx.payload)
        with session_scope() as tx:
            p = tx.get(Project, self.payload.project_id)
            if p is None or p.user_id != ctx.user_id:
                raise JobFailed(ErrorCode.not_found, "project not found")
            self.user_id, self.source_key = p.user_id, p.source_key
        self.project_id = self.payload.project_id
        self._source: Path | None = None
        self._transcript: Transcript | None = None

    def key(self, name: str) -> str:
        return storage.asset_key(self.user_id, self.project_id, name)

    def source(self) -> str:
        if self._source is None:
            path = self.work / ("source" + Path(self.source_key).suffix)
            storage.download_file(self.source_key, str(path))
            self._source = path
        return str(self._source)

    def run(self) -> Outcome:
        ctx = self.ctx
        meta = ctx.run_stage("probe", self.probe)
        self.save_probe(meta)
        ctx.run_stage("transcribe", self.transcribe)
        ctx.run_stage("proxy", self.proxy)
        ctx.run_stage("thumbnails", lambda: self.thumbnails(meta))
        ctx.run_stage("waveform", self.waveform)
        ctx.run_stage("detect", lambda: self.detect(meta))
        ctx.run_stage("speakers", lambda: self.speakers(meta))
        ctx.run_stage("index", self.index)
        self.finish()
        return Outcome.done

    # Stages

    def probe(self) -> dict[str, Any]:
        _reporter(self.ctx, "probe")(0)
        info = probe(self.source())
        if not info.has_video or info.width <= 0 or info.height <= 0:
            raise _fail_project(self.ctx, self.project_id, ErrorCode.unsupported_media, "the file has no video")
        if not info.has_audio:
            raise _fail_project(self.ctx, self.project_id, ErrorCode.unsupported_media, "the file has no audio")
        limit = _max_duration(self.user_id)
        if info.duration_s > limit:
            raise _fail_project(
                self.ctx, self.project_id, ErrorCode.duration_too_long,
                f"duration {info.duration_s:.0f} s is over the limit of {limit} s",
            )  # fmt: skip
        return {"duration_s": info.duration_s, "width": info.width, "height": info.height, "fps": info.fps}

    def save_probe(self, meta: dict[str, Any]) -> None:
        with session_scope() as tx:
            jobqueue.renew(tx, self.ctx.job_id, self.ctx.worker)
            p = tx.get(Project, self.project_id, with_for_update=True)
            if p is not None:
                p.duration_s, p.width, p.height, p.fps = (
                    meta["duration_s"], meta["width"], meta["height"], meta["fps"]
                )  # fmt: skip

    def transcribe(self) -> dict[str, Any]:
        report = _reporter(self.ctx, "transcribe")
        report(0)
        if self.payload.user_srt:
            text = storage.get_bytes(storage.user_srt_key(self.user_id, self.project_id)).decode("utf-8", "replace")
            cues = parse_srt(text)
            if not cues:
                raise _fail_project(self.ctx, self.project_id, ErrorCode.bad_request, "the SRT file has no cues")
            transcript = Transcript(language=None, cues=cues)
        else:
            transcript = self._whisper(report)
        self._transcript = transcript
        transcript_key, srt_key = self.key("transcript.json"), self.key("transcript.srt")
        storage.put_bytes(transcript_key, transcript.model_dump_json().encode(), "application/json")
        storage.put_bytes(srt_key, to_srt(transcript.cues).encode(), "application/x-subrip")
        return {"transcript_key": transcript_key, "srt_key": srt_key}

    def _whisper(self, report: Callable[[float], None]) -> Transcript:
        wav = self.work / "speech.wav"
        run_ffmpeg(commands.speech_wav(self.source(), str(wav)), register=self.ctx.register)
        self.ctx.check()
        segments, info = _whisper().transcribe(load_pcm(wav), word_timestamps=True, vad_filter=True)
        duration = float(getattr(info, "duration", 0) or 0)
        words: list[Word] = []
        for seg in segments:  # a generator: transcription happens while iterating
            self.ctx.check()
            for w in seg.words or []:
                if w.word.strip() and w.end > w.start:
                    words.append(Word(text=w.word.strip(), start=round(w.start, 3), end=round(w.end, 3)))
            if duration > 0:
                report(seg.end / duration)
        wav.unlink(missing_ok=True)
        return Transcript(language=getattr(info, "language", None), cues=group_words(words))

    def transcript(self) -> Transcript:
        if self._transcript is None:
            key = self.ctx.markers["transcribe"]["transcript_key"]
            self._transcript = Transcript.model_validate_json(storage.get_bytes(key))
        return self._transcript

    def proxy(self) -> dict[str, Any]:
        report = _reporter(self.ctx, "proxy")
        duration = self.ctx.markers["probe"]["duration_s"]
        out = self.work / "proxy.mp4"
        meta = self.ctx.markers["probe"]
        short = min(commands.PROXY_SHORT_SIDE, meta["width"], meta["height"])  # never enlarge a small source
        run_ffmpeg(
            commands.proxy(self.source(), str(out), short),
            on_time=lambda t: report(t / duration if duration else 0),
            register=self.ctx.register,
        )
        key = self.key(f"proxy-{short}p.mp4")  # a new name per quality, so browsers never show a cached old copy
        storage.upload_file(str(out), key, "video/mp4")
        out.unlink(missing_ok=True)
        return {"key": key}

    def thumbnails(self, meta: dict[str, Any]) -> dict[str, Any]:
        report = _reporter(self.ctx, "thumbnails")
        duration = meta["duration_s"]
        folder = self.work / "thumbs"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        run_ffmpeg(
            commands.sprites(self.source(), str(folder / "sprite_%03d.jpg")),
            on_time=lambda t: report(0.9 * t / duration if duration else 0),
            register=self.ctx.register,
        )
        files = sorted(folder.glob("sprite_*.jpg"))
        per_sheet = commands.SPRITE_COLUMNS * commands.SPRITE_ROWS
        total = min(max(1, math.ceil(duration / commands.THUMB_INTERVAL_S)), len(files) * per_sheet)
        sheets = []
        for i, f in enumerate(files):
            key = self.key(f"thumbs/{f.name}")
            storage.upload_file(str(f), key, "image/jpeg")
            sheets.append(
                {"key": key, "start_s": i * per_sheet * commands.THUMB_INTERVAL_S,
                 "count": max(0, min(per_sheet, total - i * per_sheet))}
            )  # fmt: skip
        poster = folder / "poster.jpg"
        run_ffmpeg(commands.poster(self.source(), str(poster), min(3.0, duration * 0.1)), register=self.ctx.register)
        poster_key = self.key("poster.jpg")
        storage.upload_file(str(poster), poster_key, "image/jpeg")
        tile_w, tile_h = commands.tile_size(meta["width"], meta["height"])
        shutil.rmtree(folder, ignore_errors=True)
        return {
            "poster_key": poster_key,
            "interval_s": commands.THUMB_INTERVAL_S,
            "tile_width": tile_w,
            "tile_height": tile_h,
            "columns": commands.SPRITE_COLUMNS,
            "rows": commands.SPRITE_ROWS,
            "sheets": sheets,
        }

    def waveform(self) -> dict[str, Any]:
        _reporter(self.ctx, "waveform")(0)
        data = waveform(self.source(), register=self.ctx.register)
        key = self.key("waveform.bin.gz")
        storage.put_bytes(key, data, "application/gzip")
        return {"key": key}

    def detect(self, meta: dict[str, Any]) -> dict[str, Any]:
        from vision import detect_people  # torch and ultralytics load only here

        report = _reporter(self.ctx, "detect")
        report(0)
        detections = detect_people(
            self.source(), meta["width"], meta["height"], meta["duration_s"],
            on_progress=report, check=self.ctx.check, register=self.ctx.register,
        )  # fmt: skip
        key = self.key("detections.json")
        storage.put_bytes(key, detections.model_dump_json().encode(), "application/json")
        return {"key": key}

    def speakers(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Who talks when (lip movement against the sound), saved as the detections plus turns."""
        report = _reporter(self.ctx, "speakers")
        report(0)
        detections = Detections.model_validate_json(storage.get_bytes(self.ctx.markers["detect"]["key"]))
        detections = with_turns(
            self.source(), meta, detections, self.transcript(), self.work,
            on_progress=report, check=self.ctx.check, register=self.ctx.register,
        )  # fmt: skip
        key = self.key(SPEAKERS_KEY_NAME)
        storage.put_bytes(key, detections.model_dump_json().encode(), "application/json")
        return {"key": key, "turns": len(detections.turns)}

    def index(self) -> dict[str, Any]:
        from rag import chunk_cues, embed_passages, write_chunks  # sentence-transformers loads only here

        _reporter(self.ctx, "index")(0)
        chunks = chunk_cues(self.transcript().cues)
        embeddings = embed_passages([c.text for c in chunks])
        self.ctx.check()
        with session_scope() as tx:
            jobqueue.renew(tx, self.ctx.job_id, self.ctx.worker)
            write_chunks(tx, self.project_id, chunks, embeddings)
        return {"chunks": len(chunks)}

    def finish(self) -> None:
        """Project ready and the ideas job enqueued, in one transaction. A second run changes nothing."""
        with session_scope() as tx:
            jobqueue.renew(tx, self.ctx.job_id, self.ctx.worker)
            p = tx.get(Project, self.project_id, with_for_update=True)
            if p is None or p.status == ProjectStatus.ready:
                return
            p.status = ProjectStatus.ready.value
            jobqueue.enqueue(
                tx, JobKind.suggest, SuggestPayload(project_id=self.project_id).model_dump(mode="json"),
                self.user_id, self.project_id,
            )  # fmt: skip


@register(JobKind.ingest)
def handle(ctx: JobContext) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="ingest-", dir=WORK_DIR) as work:
        return Ingest(ctx, Path(work)).run()
