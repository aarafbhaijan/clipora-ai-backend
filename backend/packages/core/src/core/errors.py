"""Error codes and the error envelope `{ "error": { "code", "message", "details" } }`."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    bad_request = "bad_request"
    unsupported_media = "unsupported_media"
    unauthenticated = "unauthenticated"
    forbidden = "forbidden"
    quota_exceeded = "quota_exceeded"
    not_found = "not_found"
    agent_busy = "agent_busy"
    metadata_busy = "metadata_busy"
    version_conflict = "version_conflict"
    export_in_progress = "export_in_progress"
    file_too_large = "file_too_large"
    edl_invalid = "edl_invalid"
    no_plan = "no_plan"
    fields_invalid = "fields_invalid"
    duration_too_long = "duration_too_long"
    setting_invalid = "setting_invalid"
    rate_limited = "rate_limited"
    # Job error codes (jobs.error_code), never returned as HTTP errors.
    lease_lost = "lease_lost"
    import_failed = "import_failed"  # the link's video could not be fetched (private, removed, region-locked)
    timeout = "timeout"
    internal = "internal"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.bad_request: 400,
    ErrorCode.unsupported_media: 400,
    ErrorCode.unauthenticated: 401,
    ErrorCode.forbidden: 403,
    ErrorCode.quota_exceeded: 403,
    ErrorCode.not_found: 404,
    ErrorCode.agent_busy: 409,
    ErrorCode.metadata_busy: 409,
    ErrorCode.version_conflict: 409,
    ErrorCode.export_in_progress: 409,
    ErrorCode.file_too_large: 413,
    ErrorCode.edl_invalid: 422,
    ErrorCode.no_plan: 422,
    ErrorCode.fields_invalid: 422,
    ErrorCode.duration_too_long: 422,
    ErrorCode.setting_invalid: 422,
    ErrorCode.rate_limited: 429,
}


class AppError(Exception):
    """An expected failure with a stable code. The API turns it into the error envelope."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code.value, "message": self.message, "details": self.details}}
