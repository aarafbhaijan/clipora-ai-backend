"""Run ffmpeg and ffprobe as child processes. Arguments are always lists, never shell strings."""

import json
import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")

# Every ffmpeg command starts with these. Progress goes to stdout as key=value lines.
BASE = ["-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]


class MediaError(Exception):
    """ffmpeg or ffprobe failed. `detail` holds the tail of stderr."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(f"{message}: {detail}" if detail else message)
        self.detail = detail


def ffmpeg(*args: str) -> list[str]:
    return [FFMPEG, *BASE, *args]


def ffmpeg_pipe(*args: str) -> list[str]:
    """For commands that write media to stdout: no progress lines there."""
    return [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args]


def run_ffmpeg(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    on_time: Callable[[float], None] | None = None,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> None:
    """Run one ffmpeg command. `on_time` gets the output position in seconds as it advances.

    `register` receives the process so a cancelled job can kill it.
    """
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if register is not None:
        register(proc)
    assert proc.stdout is not None and proc.stderr is not None
    err_tail: deque[str] = deque(maxlen=20)

    def drain() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            err_tail.append(raw.decode(errors="replace").rstrip())

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if on_time is not None and line.startswith("out_time_us="):
            value = line.split("=", 1)[1]
            if value.isdigit():
                on_time(int(value) / 1_000_000)
    code = proc.wait()
    t.join(timeout=5)
    if code != 0:
        raise MediaError(f"ffmpeg exited with {code}", "\n".join(err_tail))


def run_json(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        raise MediaError("ffprobe failed", proc.stderr.decode(errors="replace")[-2000:])
    return dict(json.loads(proc.stdout or b"{}"))
