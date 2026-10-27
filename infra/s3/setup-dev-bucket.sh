#!/usr/bin/env bash
# Create and configure the S3 bucket for Clipora (architecture.md sections 5 and 15).
#   AWS_PROFILE=personal infra/s3/setup-dev-bucket.sh <bucket-name> <region> <web-origin>
#   e.g. AWS_PROFILE=personal infra/s3/setup-dev-bucket.sh clipora-dev-media-22e7cc ap-south-1 http://localhost:3001
# Safe to re-run: every step is idempotent. Needs the permissions in deployer-policy.json.
set -euo pipefail

BUCKET="${1:?bucket name}"
REGION="${2:?region}"
ORIGIN="${3:?web origin, e.g. http://localhost:3001}"

if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Bucket $BUCKET exists"
else
  echo "Creating $BUCKET in $REGION"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --object-ownership BucketOwnerEnforced
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" --object-ownership BucketOwnerEnforced
  fi
fi

echo "Blocking all public access"
aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "Default encryption (SSE-S3)"
aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

echo "Lifecycle: abort unfinished multipart uploads after 1 day"
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" --lifecycle-configuration \
  '{"Rules":[{"ID":"abort-incomplete-mpu","Status":"Enabled","Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'

echo "CORS for $ORIGIN (browser uploads parts with PUT and reads media with GET)"
aws s3api put-bucket-cors --bucket "$BUCKET" --region "$REGION" --cors-configuration "{
  \"CORSRules\": [{
    \"AllowedOrigins\": [\"$ORIGIN\"],
    \"AllowedMethods\": [\"GET\", \"PUT\", \"HEAD\"],
    \"AllowedHeaders\": [\"*\"],
    \"ExposeHeaders\": [\"ETag\"],
    \"MaxAgeSeconds\": 3600
  }]
}"

echo "Done. Set in .env: S3_BUCKET=$BUCKET S3_REGION=$REGION, and remove S3_ENDPOINT_URL, S3_PUBLIC_ENDPOINT_URL,"
echo "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY so the app uses your AWS credentials."
