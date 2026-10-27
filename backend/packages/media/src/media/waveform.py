"""Audio waveform for the editor (ingest stage 5).

Format of `waveform.bin.gz` (gzip): signed int8 pairs `min, max` per 50 ms bucket, 20 buckets per second,
interleaved as [min0, max0, min1, max1, ...]. Values are 16-bit samples divided by 256.
"""

import gzip
import subprocess
import sys
from array import array
from collections.abc import Callable, Iterable

from media.commands import WAVEFORM_RATE, waveform_pcm
from media.ff import MediaError

BUCKETS_PER_S = 20
BUCKET = WAVEFORM_RATE // BUCKETS_PER_S  # samples per bucket


def _to_i8(v: int) -> int:
    return max(-128, min(127, v // 256))


def buckets(chunks: Iterable[bytes]) -> bytes:
    """Min/max int8 pairs from a stream of little-endian s16 mono bytes."""
    out = array("b")
    carry = b""
    for chunk in chunks:
        data = carry + chunk
        whole = len(data) - len(data) % (BUCKET * 2)
        carry = data[whole:]
        samples = array("h")
        samples.frombytes(data[:whole])
        if sys.byteorder == "big":
            samples.byteswap()
        for i in range(0, len(samples), BUCKET):
            part = samples[i : i + BUCKET]
            out.append(_to_i8(min(part)))
            out.append(_to_i8(max(part)))
    if len(carry) >= 2:
        samples = array("h")
        samples.frombytes(carry[: len(carry) - len(carry) % 2])
        if sys.byteorder == "big":
            samples.byteswap()
        out.append(_to_i8(min(samples)))
        out.append(_to_i8(max(samples)))
    return out.tobytes()


def waveform(
    src: str,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> bytes:
    """Decode the audio and return the gzipped waveform."""
    proc = subprocess.Popen(waveform_pcm(src), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if register is not None:
        register(proc)
    assert proc.stdout is not None

    def read() -> Iterable[bytes]:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(1 << 16):
            yield chunk

    data = buckets(read())
    _, err = proc.communicate()
    if proc.returncode != 0:
        raise MediaError("waveform decode failed", err.decode(errors="replace")[-2000:])
    return gzip.compress(data, mtime=0)
