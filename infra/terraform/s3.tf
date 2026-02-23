############################################################
# S3 — Document Storage
############################################################

resource "aws_kms_key" "documents" {
  description             = "${local.name_prefix} document encryption key"
  deletion_window_in_days = 14
  enable_key_rotation     = true

  tags = { Name = "${local.name_prefix}-docs-kms" }
}

resource "aws_kms_alias" "documents" {
  name          = "alias/${local.name_prefix}-documents"
  target_key_id = aws_kms_key.documents.key_id
}

resource "aws_s3_bucket" "documents" {
  bucket        = "${local.name_prefix}-documents-${local.suffix}"
  force_destroy = var.environment != "prod"

  tags = { Name = "${local.name_prefix}-documents" }
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.documents.arn
    }
    bucket_key_enabled = true  # Reduce KMS API calls by 99%
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "expire-failed-uploads"
    status = "Enabled"

    filter { prefix = "tmp/" }

    expiration { days = 1 }
  }

  rule {
    id     = "transition-old-versions"
    status = "Enabled"

    filter { prefix = "" }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }

    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

resource "aws_s3_bucket_cors_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = ["*"]   # Restrict to your domain in prod
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# ── Terraform State Bucket (bootstrap separately) ────────────
# NOTE: This bucket must already exist before running terraform init.
# Run: infra/scripts/bootstrap-state.sh to create it.
