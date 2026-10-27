import time
from collections.abc import Iterator

import pytest

from chat.heartbeat import PING, with_heartbeat
from chat.turn import sse


def slow() -> Iterator[str]:
    yield "a"
    time.sleep(0.35)
    yield "b"


def test_pings_fill_silence_and_events_keep_order() -> None:
    out = list(with_heartbeat(slow(), every_s=0.1))
    assert out[0] == "a" and out[-1] == "b"
    assert out.count(PING) >= 2


def test_errors_from_the_stream_are_raised() -> None:
    def broken() -> Iterator[str]:
        yield "a"
        raise RuntimeError("model failed")

    with pytest.raises(RuntimeError):
        list(with_heartbeat(broken(), every_s=0.1))


def test_sse_format() -> None:
    assert sse("token", {"text": "hi"}) == 'event: token\ndata: {"text": "hi"}\n\n'
