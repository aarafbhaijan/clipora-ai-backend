"""Edit ops: how the agent revises a plan. The LLM returns ops, code applies them (architecture.md 11.1)."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from core.enums import Framing
from core.transitions import TransitionType


class RemoveOp(BaseModel):
    op: Literal["remove"] = "remove"
    id: str


class TrimOp(BaseModel):
    op: Literal["trim"] = "trim"
    id: str
    src_start: float | None = None
    src_end: float | None = None


class AddOp(BaseModel):
    op: Literal["add"] = "add"
    src_start: float
    src_end: float
    after_id: str | None = None  # None adds at the start


class ReorderOp(BaseModel):
    op: Literal["reorder"] = "reorder"
    ids: list[str]


class SetTargetOp(BaseModel):
    op: Literal["set_target"] = "set_target"
    seconds: float


class SetTransitionOp(BaseModel):
    op: Literal["set_transition"] = "set_transition"
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    all: bool = False
    type: TransitionType
    duration_s: float = 0.4


class SetAspectOp(BaseModel):
    op: Literal["set_aspect"] = "set_aspect"
    aspect: str | None = None
    width: int | None = None
    height: int | None = None
    framing: Framing | None = None


EditOp = Annotated[
    RemoveOp | TrimOp | AddOp | ReorderOp | SetTargetOp | SetTransitionOp | SetAspectOp,
    Field(discriminator="op"),
]
