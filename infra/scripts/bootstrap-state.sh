#!/usr/bin/env bash
##############################################################
# bootstrap-state.sh
# Creates the S3 bucket + DynamoDB table for Terraform state.
# Run this ONCE before `terraform init`.
##############################################################
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT_NAME:-rag-platform}"
BUCKET_NAME="${TF_STATE_BUCKET:-${PROJECT}-tfstate}"
TABLE_NAME="${TF_LOCK_TABLE:-${PROJECT}-tfstate-lock}"

echo "=== Bootstrapping Terraform State Backend ==="
echo "Region:  $AWS_REGION"
echo "Bucket:  $BUCKET_NAME"
echo "Table:   $TABLE_NAME"
echo ""

# ── Create S3 bucket ──────────────────────────────────────────
if aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
  echo "[SKIP] Bucket $BUCKET_NAME already exists"
else
  echo "Creating S3 bucket: $BUCKET_NAME"
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION"
  else
    aws s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
fi

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled

# Block public access
aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Enable SSE
aws s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

echo "[OK] S3 bucket configured"

# ── Create DynamoDB table for state locking ───────────────────
if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" 2>/dev/null; then
  echo "[SKIP] DynamoDB table $TABLE_NAME already exists"
else
  echo "Creating DynamoDB table: $TABLE_NAME"
  aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION"

  aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$AWS_REGION"
fi

echo "[OK] DynamoDB table configured"
echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Now update infra/terraform/main.tf backend config:"
echo "  bucket         = \"$BUCKET_NAME\""
echo "  dynamodb_table = \"$TABLE_NAME\""
echo "  region         = \"$AWS_REGION\""
echo ""
echo "Then run: cd infra/terraform && terraform init"
