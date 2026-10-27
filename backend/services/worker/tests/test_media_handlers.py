import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused@localhost/unused")

from worker.handlers import ingest  # noqa: E402


def test_stage_weights_cover_the_bar() -> None:
    assert sum(ingest.WEIGHTS.values()) == 100
    assert list(ingest.WEIGHTS) == [
        "probe",
        "transcribe",
        "proxy",
        "thumbnails",
        "waveform",
        "detect",
        "speakers",
        "index",
    ]
    assert ingest._base("transcribe") == 3 and ingest._base("index") == 90


def test_handlers_register() -> None:
    from core.enums import JobKind
    from worker import handlers
    from worker.handlers import export  # noqa: F401

    assert handlers.handler_for(JobKind.ingest.value) is ingest.handle
    assert handlers.handler_for(JobKind.export.value) is not None


@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="downloads the Whisper small model; set RUN_SLOW=1")
def test_whisper_loads(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000",
         "-ac", "1", str(wav)],
        check=True,
    )  # fmt: skip
    segments, info = ingest._whisper().transcribe(ingest.load_pcm(wav), word_timestamps=True, vad_filter=True)
    list(segments)
    assert info.duration == pytest.approx(2.0, abs=0.1)
