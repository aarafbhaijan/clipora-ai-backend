"""Copy every object from the local S3 server (RustFS) to the real S3 bucket, so existing videos keep working.

    AWS_PROFILE=personal uv run --no-sync python infra/s3/copy_local_to_s3.py \\
        --src-endpoint http://localhost:9100 --src-bucket clipora-local \\
        --dst-bucket clipora-dev-media-22e7cc --dst-region ap-south-1

Object keys are the same in both, so the database needs no change. Safe to re-run: objects that already exist
in the destination with the same size are skipped.
"""

import argparse
import os
import tempfile

import boto3
from botocore.config import Config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-endpoint", default="http://localhost:9100")
    ap.add_argument("--src-bucket", default="clipora-local")
    ap.add_argument("--src-key-id", default=os.environ.get("S3_ACCESS_KEY_ID", "clipora"))
    ap.add_argument("--src-secret", default=os.environ.get("S3_SECRET_ACCESS_KEY", "clipora-secret"))
    ap.add_argument("--dst-bucket", required=True)
    ap.add_argument("--dst-region", required=True)
    a = ap.parse_args()

    src = boto3.client(
        "s3",
        endpoint_url=a.src_endpoint,
        region_name="us-east-1",
        aws_access_key_id=a.src_key_id,
        aws_secret_access_key=a.src_secret,
        config=Config(s3={"addressing_style": "path"}),
    )
    dst = boto3.client("s3", region_name=a.dst_region)  # default credential chain (AWS_PROFILE)

    copied = skipped = 0
    for page in src.get_paginator("list_objects_v2").paginate(Bucket=a.src_bucket):
        for obj in page.get("Contents", []):
            key, size = obj["Key"], obj["Size"]
            try:
                if dst.head_object(Bucket=a.dst_bucket, Key=key)["ContentLength"] == size:
                    skipped += 1
                    continue
            except dst.exceptions.ClientError:
                pass
            head = src.head_object(Bucket=a.src_bucket, Key=key)
            with tempfile.NamedTemporaryFile() as f:
                src.download_fileobj(a.src_bucket, key, f)
                f.flush()
                dst.upload_file(
                    f.name, a.dst_bucket, key, ExtraArgs={"ContentType": head.get("ContentType", "binary/octet-stream")}
                )
            copied += 1
            print(f"copied {key} ({size / 1e6:.1f} MB)")
    print(f"done: {copied} copied, {skipped} already there")


if __name__ == "__main__":
    main()
