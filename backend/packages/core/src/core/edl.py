"""The EDL (edit plan). The only contract between agent, API, preview and export (architecture.md section 7).

Change this file first, then the API, the agent, the preview and the export, with tests, in the same change.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.enums import Framing
from core.transitions import TransitionType


class CropKeyframe(BaseModel):
    """Crop window centre in source time. cx and cy are fractions of the source frame."""

    t: float
    cx: float
    cy: float
    zoom: float = 1.0


class Segment(BaseModel):
    id: str = Field(pattern=r"^s[0-9]+$")
    src_start: float
    src_end: float
    reason: str | None = None
    score: float | None = None
    crop: list[CropKeyframe] = Field(default_factory=list)

    @property
    def length(self) -> float:
        return self.src_end - self.src_start


class Source(BaseModel):
    key: str
    duration_s: float
    width: int
    height: int
    fps: float


class Output(BaseModel):
    aspect: str = "9:16"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    framing: Framing = Framing.crop
    target_duration_s: float | None = None


class Transition(BaseModel):
    type: TransitionType = TransitionType.fade
    duration_s: float = 0.4

    @property
    def effective_duration(self) -> float:
        """Overlap in seconds. A cut has none, whatever duration it carries."""
        return 0.0 if self.type == TransitionType.cut else self.duration_s


class TransitionOverride(Transition):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class Transitions(BaseModel):
    default: Transition = Field(default_factory=Transition)
    overrides: list[TransitionOverride] = Field(default_factory=list)


class Captions(BaseModel):
    enabled: bool = True
    style: str = "bold-center"
    edits: dict[str, str] = Field(default_factory=dict)


class Candidate(BaseModel):
    src_start: float
    src_end: float
    score: float | None = None
    reason: str | None = None


class RunInfo(BaseModel):
    prompt_version: str
    model: str


class Meta(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidates: list[Candidate] = Field(default_factory=list)
    run: RunInfo | None = None


class EDL(BaseModel):
    version: int = 0
    source: Source
    output: Output = Field(default_factory=Output)
    segments: list[Segment]
    transitions: Transitions = Field(default_factory=Transitions)
    audio_fade_ms: int = 80
    captions: Captions = Field(default_factory=Captions)
    meta: Meta = Field(default_factory=Meta)

    def boundary(self, i: int) -> Transition:
        """Transition between segments[i] and segments[i + 1]: an override if one names them, else the default."""
        a, b = self.segments[i].id, self.segments[i + 1].id
        for o in self.transitions.overrides:
            if o.from_ == a and o.to == b:
                return Transition(type=o.type, duration_s=o.duration_s)
        return self.transitions.default

    def boundaries(self) -> list[Transition]:
        return [self.boundary(i) for i in range(len(self.segments) - 1)]

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
