"""Postgres checkpointer for the LangGraph graphs (one pool per worker process).

The pool is sized by AGENT_CHECKPOINT_POOL (default 4, one per agent slot). Count it in the
database connection budget (architecture.md section 9): worker slots + 2 + this pool.
`setup()` creates LangGraph's own checkpoint tables on first start; they are managed by LangGraph,
not by our Alembic migrations.
"""

import os
import threading

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from db.session import database_url

_saver: PostgresSaver | None = None
_lock = threading.Lock()


def checkpointer() -> PostgresSaver:
    global _saver
    with _lock:
        if _saver is None:
            url = database_url().replace("postgresql+psycopg://", "postgresql://", 1)
            pool: ConnectionPool = ConnectionPool(
                url,
                min_size=1,
                max_size=int(os.environ.get("AGENT_CHECKPOINT_POOL", "4")),
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=True,
            )
            saver = PostgresSaver(pool)  # type: ignore[arg-type]
            saver.setup()
            _saver = saver
    return _saver
