"""Create the local S3 bucket in LocalStack (local development only): python -m storage.local_setup"""

import os

from storage import bucket, client
from storage import region as region_name

if __name__ == "__main__":
    if not os.environ.get("S3_ENDPOINT_URL"):
        raise SystemExit("S3_ENDPOINT_URL is not set: this is for local S3 (LocalStack) only.")
    existing = {b["Name"] for b in client().list_buckets().get("Buckets", [])}
    if bucket() not in existing:
        region = region_name()
        extra = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        client().create_bucket(Bucket=bucket(), **extra)
    client().put_bucket_lifecycle_configuration(
        Bucket=bucket(),
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "abort-mpu",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    )
    client().put_bucket_cors(
        Bucket=bucket(),
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": [os.environ.get("WEB_ORIGIN", "http://localhost:3000")],
                    "AllowedMethods": ["GET", "PUT", "HEAD"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    print(f"bucket {bucket()} ready")
