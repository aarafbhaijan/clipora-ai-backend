"""S3 key builders, presign and multipart helpers (architecture.md sections 5 and 15).

Uses the default AWS credential chain (ECS task role on AWS).
Locally, S3_ENDPOINT_URL and S3_ACCESS_KEY_ID point at LocalStack.
"""

import os
import threading
import time
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

PART_SIZE_MB = 16
UPLOAD_URL_TTL_S = 15 * 60
ASSET_URL_TTL_S = 60 * 60


def bucket() -> str:
    name = os.environ.get("S3_BUCKET")
    if not name:
        raise RuntimeError("S3_BUCKET is not set")
    return name


def region() -> str:
    """Bucket region. S3_REGION wins so S3 can live in another region than Bedrock (AWS_REGION)."""
    return os.environ.get("S3_REGION") or os.environ.get("AWS_REGION", "us-east-1")


def _local_keys() -> dict[str, str]:
    """Keys for a local S3-compatible server (RustFS) only. On AWS these are unset: the profile or task role is used."""
    key, secret = os.environ.get("S3_ACCESS_KEY_ID"), os.environ.get("S3_SECRET_ACCESS_KEY")
    return {"aws_access_key_id": key, "aws_secret_access_key": secret} if key and secret else {}


def _make_client(endpoint_url: str | None) -> Any:
    # Real S3: virtual-hosted, regional URLs (what CORS and presigned uploads expect).
    # A local server (endpoint_url set) needs path-style addressing.
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region(),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path" if endpoint_url else "virtual"}),
        **_local_keys(),
    )


@lru_cache(maxsize=1)
def client() -> Any:
    return _make_client(os.environ.get("S3_ENDPOINT_URL") or None)


@lru_cache(maxsize=1)
def _public_client() -> Any:
    """Client for URLs handed to the browser. Locally the browser may reach the server on another host name."""
    public = os.environ.get("S3_PUBLIC_ENDPOINT_URL")
    return _make_client(public) if public else client()


# Keys. Uploads are scoped to uploads/{user}/{project}/ so presigned URLs cannot write elsewhere.


def source_key(user_id: uuid.UUID, project_id: uuid.UUID, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    return f"uploads/{user_id}/{project_id}/source.{ext}"


def user_srt_key(user_id: uuid.UUID, project_id: uuid.UUID) -> str:
    return f"uploads/{user_id}/{project_id}/user.srt"


def asset_prefix(user_id: uuid.UUID, project_id: uuid.UUID) -> str:
    return f"assets/{user_id}/{project_id}/"


def asset_key(user_id: uuid.UUID, project_id: uuid.UUID, name: str) -> str:
    return asset_prefix(user_id, project_id) + name


def export_key(project_id: uuid.UUID, export_id: uuid.UUID, ext: str = "mp4") -> str:
    return f"exports/{project_id}/{export_id}.{ext}"


# Multipart upload from the browser.


def start_multipart(key: str, content_type: str) -> str:
    resp = client().create_multipart_upload(Bucket=bucket(), Key=key, ContentType=content_type)
    return str(resp["UploadId"])


def part_urls(key: str, upload_id: str, part_numbers: list[int]) -> dict[int, str]:
    return {
        n: _public_client().generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket(), "Key": key, "UploadId": upload_id, "PartNumber": n},
            ExpiresIn=UPLOAD_URL_TTL_S,
        )
        for n in part_numbers
    }


def complete_multipart(key: str, upload_id: str) -> int:
    """Complete from the parts S3 has. Returns the object size in bytes."""
    parts: list[dict[str, Any]] = []
    paginator = client().get_paginator("list_parts")
    for page in paginator.paginate(Bucket=bucket(), Key=key, UploadId=upload_id):
        parts.extend({"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in page.get("Parts", []))
    client().complete_multipart_upload(Bucket=bucket(), Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
    return object_size(key)


def object_size(key: str) -> int:
    return int(client().head_object(Bucket=bucket(), Key=key)["ContentLength"])


def abort_multipart(key: str, upload_id: str) -> None:
    client().abort_multipart_upload(Bucket=bucket(), Key=key, UploadId=upload_id)


# Objects.


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    client().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)


def get_bytes(key: str) -> bytes:
    return bytes(client().get_object(Bucket=bucket(), Key=key)["Body"].read())


def exists(key: str) -> bool:
    try:
        client().head_object(Bucket=bucket(), Key=key)
        return True
    except client().exceptions.ClientError:
        return False


def download_file(key: str, path: str) -> None:
    client().download_file(bucket(), key, path)


def upload_file(path: str, key: str, content_type: str) -> None:
    """boto3 switches to multipart above 8 MB on its own."""
    client().upload_file(path, bucket(), key, ExtraArgs={"ContentType": content_type})


def delete_prefix(prefix: str) -> None:
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client().delete_objects(Bucket=bucket(), Delete={"Objects": keys})


# Signed read URLs are reused until they are 15 minutes from expiry. Polling clients get the same URL on
# every response, so the browser never reloads a video or re-downloads thumbnails just because of a poll.
_URL_REUSE_S = ASSET_URL_TTL_S - 15 * 60
_url_cache: dict[tuple[str, str | None], tuple[str, float]] = {}
_url_lock = threading.Lock()


def asset_url(key: str, download_name: str | None = None) -> str:
    """Short-lived read URL for the browser, stable across polls.

    v1 uses presigned GETs. CloudFront signed cookies (architecture.md section 5) replace this
    once the distribution and its key pair exist.
    """
    now = time.monotonic()
    with _url_lock:
        hit = _url_cache.get((key, download_name))
        if hit and now - hit[1] < _URL_REUSE_S:
            return hit[0]
    params: dict[str, Any] = {"Bucket": bucket(), "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    url = str(_public_client().generate_presigned_url("get_object", Params=params, ExpiresIn=ASSET_URL_TTL_S))
    with _url_lock:
        if len(_url_cache) > 10_000:
            _url_cache.clear()
        _url_cache[(key, download_name)] = (url, now)
    return url
