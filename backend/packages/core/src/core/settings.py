"""Application limits stored in `app_settings`, overridable per user in `user_limits` (architecture.md section 16).

The effective value for a user is their override, else the global row, else the default here.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core.errors import AppError, ErrorCode


class LimitKey(StrEnum):
    max_active_videos = "max_active_videos"
    max_file_size_mb = "max_file_size_mb"
    max_storage_mb = "max_storage_mb"
    max_video_duration_s = "max_video_duration_s"


@dataclass(frozen=True)
class LimitSpec:
    key: LimitKey
    default: int
    minimum: int
    maximum: int
    unit: str
    label: str


LIMITS: dict[LimitKey, LimitSpec] = {
    s.key: s
    for s in (
        LimitSpec(LimitKey.max_active_videos, 5, 1, 1000, "videos", "Videos per user"),
        LimitSpec(LimitKey.max_file_size_mb, 1024, 10, 51200, "MB", "Maximum file size"),
        LimitSpec(LimitKey.max_storage_mb, 5120, 10, 1048576, "MB", "Storage per user"),
        LimitSpec(LimitKey.max_video_duration_s, 3600, 10, 14400, "seconds", "Maximum video length"),
    )
}


def check_value(key: str, value: Any) -> int:
    """Validate one setting value. Raises `setting_invalid`."""
    try:
        spec = LIMITS[LimitKey(key)]
    except ValueError as e:
        raise AppError(ErrorCode.setting_invalid, f"Unknown setting {key}.", {"key": key}) from e
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(ErrorCode.setting_invalid, f"{spec.label} must be a whole number.", {"key": key})
    if not spec.minimum <= value <= spec.maximum:
        raise AppError(
            ErrorCode.setting_invalid,
            f"{spec.label} must be between {spec.minimum} and {spec.maximum} {spec.unit}.",
            {"key": key, "minimum": spec.minimum, "maximum": spec.maximum},
        )
    return value


def effective_limits(global_values: dict[str, Any], overrides: dict[str, Any]) -> dict[LimitKey, int]:
    result: dict[LimitKey, int] = {}
    for key, spec in LIMITS.items():
        if key.value in overrides:
            result[key] = int(overrides[key.value])
        elif key.value in global_values:
            result[key] = int(global_values[key.value])
        else:
            result[key] = spec.default
    return result
