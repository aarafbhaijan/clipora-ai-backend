"""Request and response bodies. The frontend generates its types from the OpenAPI file built from these."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.edl import EDL
from core.enums import Role
from core.settings import LimitKey

HistoryState = Literal[
    "uploading",
    "waiting",
    "processing",
    "finding_ideas",
    "pick_idea",
    "planning",
    "needs_answer",
    "exporting",
    "ready",
    "exported",
    "failed",
]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    stage: str | None
    percent: int
    error_code: str | None
    error_detail: str | None = None
    created_at: datetime


class ExportBrief(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime


class ProjectListItem(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    state: HistoryState
    created_at: datetime
    updated_at: datetime
    duration_s: float | None
    thumbnail_url: str | None
    active_job: JobOut | None
    failed_job: JobOut | None
    last_export: ExportBrief | None


class ProjectPage(BaseModel):
    items: list[ProjectListItem]
    next_cursor: str | None


class CreateProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str
    duration_s: float | None = Field(default=None, gt=0)
    srt_text: str | None = Field(default=None, max_length=2_000_000)


class CreateProjectOut(BaseModel):
    project_id: uuid.UUID
    upload_id: str
    part_size_mb: int
    part_count: int


class ReframeIn(BaseModel):
    aspect: str = Field(default="9:16", pattern=r"^\d{1,3}:\d{1,3}$")
    framing: Literal["crop", "fit"] = "crop"


class ImportIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class ImportOut(BaseModel):
    project_id: uuid.UUID
    job: JobOut


class PartUrlsIn(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=100)


class PartUrlsOut(BaseModel):
    urls: dict[int, str]


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageOut(BaseModel):
    id: uuid.UUID
    thread: str
    role: str
    kind: str
    content: str
    payload: dict[str, Any]
    edl_version: int | None
    created_at: datetime


class SpriteSheet(BaseModel):
    url: str
    start_s: float
    count: int


class Thumbnails(BaseModel):
    interval_s: float
    tile_width: int
    tile_height: int
    columns: int
    rows: int
    sheets: list[SpriteSheet]


class Assets(BaseModel):
    proxy_url: str | None = None
    source_url: str | None = None  # the original upload, for the editor's full-quality preview
    waveform_url: str | None = None
    transcript_url: str | None = None
    srt_url: str | None = None
    thumbnails: Thumbnails | None = None


class ProjectDetail(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    state: HistoryState
    duration_s: float | None
    width: int | None
    height: int | None
    fps: float | None
    current_version: int
    created_at: datetime
    updated_at: datetime
    active_job: JobOut | None
    failed_job: JobOut | None
    last_export: ExportBrief | None
    messages: list[MessageOut]
    edl: EDL | None
    assets: Assets


class SendMessageIn(BaseModel):
    content: str = Field(default="", max_length=4000)
    suggestion_id: str | None = None
    choice: str | None = Field(default=None, max_length=500)


class SendMessageOut(BaseModel):
    message: MessageOut
    job: JobOut


class SpeakerBox(BaseModel):
    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float


class SpeakerFrame(BaseModel):
    t: float
    boxes: list[SpeakerBox]


class SpeakerTurnOut(BaseModel):
    """A stretch of source time in which one person is talking (lip movement matched to the sound)."""

    start: float
    end: float
    track_id: int
    label: str  # "Person 1", "Person 2"... in order of first talking
    confidence: float


class SpeakerOut(BaseModel):
    """YOLO person boxes (fractions of the source frame), the main person the crop follows when nobody is
    talking, and who talks when (the crop cuts to them)."""

    available: bool
    fps: float = 0
    main_track_id: int | None = None
    coverage: float = 0  # share of frames (inside the plan's clips, or the whole video) where the speaker is seen
    people: int = 0  # distinct tracked people seen for at least a second
    frames: list[SpeakerFrame] = []
    analysed: bool = False  # the "who talks when" step ran for this video (videos from before it have none)
    turns: list[SpeakerTurnOut] = []
    cut_min_s: float = 0  # turns shorter than this don't move the crop (core.crop.CUT_MIN_S)


class EdlOut(BaseModel):
    version: int
    edl: EDL


class SegmentBrief(BaseModel):
    id: str
    src_start: float
    src_end: float


class EdlVersionOut(BaseModel):
    version: int
    author: str
    parent_version: int | None
    created_at: datetime
    segment_count: int
    output_duration_s: float
    # The generated video this version belongs to (edl.meta.short, inherited from the parent when missing).
    short_id: str
    short_title: str
    aspect: str
    segments: list[SegmentBrief]


class ExportIn(BaseModel):
    edl_version: int | None = None


class ExportOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    edl_version: int
    status: str
    percent: int
    stage: str | None
    error_code: str | None
    download_url: str | None
    srt_url: str | None
    created_at: datetime


class MetadataStartIn(BaseModel):
    platform: str | None = None
    language: str | None = None
    tone: Literal["neutral", "casual", "punchy", "professional"] | None = None
    notes: str | None = Field(default=None, max_length=1000)


class MetadataMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class MetadataSetOut(BaseModel):
    version: int
    edl_version: int
    platform: str | None
    language: str | None
    tone: str | None
    fields: dict[str, Any]
    author: str
    created_at: datetime


class MetadataOut(BaseModel):
    current: MetadataSetOut | None
    versions: list[MetadataSetOut]
    messages: list[MessageOut]
    active_job: JobOut | None
    stale: bool


class MetadataPatchIn(BaseModel):
    fields: dict[str, Any]
    platform: str | None = None


class FeedbackIn(BaseModel):
    target_kind: Literal["plan", "idea", "metadata", "answer"]
    target_ref: str = Field(max_length=128)
    signal: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=500)


class SearchHit(BaseModel):
    cue_from: str
    cue_to: str
    start_s: float
    end_s: float
    text: str


class MemoryOut(BaseModel):
    id: uuid.UUID
    kind: str
    text: str
    status: str
    evidence_count: int
    created_at: datetime
    updated_at: datetime


class MemoryPatchIn(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=300)
    status: Literal["active", "disabled"] | None = None


class Usage(BaseModel):
    active_videos: int
    storage_bytes: int


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    learn_enabled: bool
    limits: dict[LimitKey, int]
    usage: Usage


class MePatchIn(BaseModel):
    learn_enabled: bool


# Admin


class LimitSpecOut(BaseModel):
    key: LimitKey
    label: str
    unit: str
    default: int
    minimum: int
    maximum: int
    value: int


class AdminSettingsOut(BaseModel):
    limits: list[LimitSpecOut]


class AdminSettingsPatchIn(BaseModel):
    values: dict[str, int]


class TokenTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: Decimal | None = None
    unpriced_calls: int = 0


class AdminUserRow(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    created_at: datetime
    active_videos: int
    storage_bytes: int
    tokens: TokenTotals


class AdminUserPage(BaseModel):
    items: list[AdminUserRow]
    next_cursor: str | None


class UsageGroup(BaseModel):
    key: str
    label: str
    tokens: TokenTotals


class AdminUserDetail(BaseModel):
    user: AdminUserRow
    limits: dict[LimitKey, int]
    overrides: dict[LimitKey, int]
    by_project: list[UsageGroup]
    by_kind: list[UsageGroup]
    by_day: list[UsageGroup]


class AdminUserPatchIn(BaseModel):
    role: Role | None = None
    overrides: dict[str, int | None] | None = None


class AdminUsageOut(BaseModel):
    total: TokenTotals
    groups: list[UsageGroup]
