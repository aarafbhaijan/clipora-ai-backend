"""Keep a slow SSE stream alive. Proxies (Next.js dev, ALB, CloudFront) close connections that stay silent too
long; a slow model (local Ollama) can be silent for a minute while it reads the transcript."""

import queue
import threading
from collections.abc import Iterator

PING = ": ping\n\n"  # an SSE comment: browsers ignore it, proxies see traffic
_DONE = object()


def with_heartbeat(events: Iterator[str], every_s: float = 10.0) -> Iterator[str]:
    """Yield from `events` (run in a worker thread), adding a ping whenever nothing arrived for `every_s`."""
    q: queue.Queue[object] = queue.Queue()

    def pump() -> None:
        try:
            for item in events:
                q.put(item)
        except BaseException as e:  # surfaced in the consumer below
            q.put(e)
        finally:
            q.put(_DONE)

    threading.Thread(target=pump, name="chat-turn", daemon=True).start()
    while True:
        try:
            item = q.get(timeout=every_s)
        except queue.Empty:
            yield PING
            continue
        if item is _DONE:
            return
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, str)
        yield item
