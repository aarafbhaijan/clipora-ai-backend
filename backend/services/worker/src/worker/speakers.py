"""Who talks when, for one video (architecture.md section 12.1). Used by the ingest "speakers" stage and by
`python -m worker.backfill_speakers` for videos ingested before the stage existed.

The result is the detections with `turns` added, stored under a new key: detections.json is cached by key in the
API and the agent, so it is never rewritten in place.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.crop import Detections, track_sizes
from core.speakers import speaker_turns
from core.transcript import Transcript, Word
from media import commands
from media.ff import MediaError, run_ffmpeg

KEY_NAME = "detections-speakers.json"


def speech_words(transcript: Transcript) -> list[Word]:
    """Word timings, or whole cues when the transcript came from an uploaded SRT without words."""
    words = [w for c in transcript.cues for w in c.words]
    return words or [Word(text=c.text, start=c.start, end=c.end) for c in transcript.cues]


def _pcm(src: str, work: Path, register: Callable[[subprocess.Popen[bytes]], None] | None) -> Any | None:
    """16 kHz mono samples of the first audio stream, or None when the video has no audio."""
    from worker.handlers.ingest import load_pcm

    wav = work / "speakers.wav"
    try:
        run_ffmpeg(commands.speech_wav(src, str(wav)), register=register)
        return load_pcm(wav)
    except MediaError:
        return None
    finally:
        wav.unlink(missing_ok=True)


def with_turns(
    src: str,
    meta: dict[str, Any],
    detections: Detections,
    transcript: Transcript,
    work: Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    check: Callable[[], None] | None = None,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Detections:
    """The detections with speaker turns. No people, no speech or no audio: no turns (the crop then follows the
    main person, as before)."""
    words = speech_words(transcript)
    if not words or not any(f.boxes for f in detections.frames):
        return detections.model_copy(update={"turns": []})
    pcm = _pcm(src, work, register)
    if pcm is None:
        return detections.model_copy(update={"turns": []})
    from vision.lips import measure_lips  # mediapipe loads only here

    lips = measure_lips(
        src, meta["width"], meta["height"], meta["duration_s"], detections, pcm,
        on_progress=on_progress, check=check, register=register,
    )  # fmt: skip
    return detections.model_copy(update={"turns": speaker_turns(lips, words, track_sizes(detections))})
