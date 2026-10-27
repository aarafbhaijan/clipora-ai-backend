"""Request-scoped dependencies: one database transaction per request."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from db.session import session_scope


def get_tx() -> Iterator[Session]:
    with session_scope() as tx:
        yield tx


Tx = Annotated[Session, Depends(get_tx)]
