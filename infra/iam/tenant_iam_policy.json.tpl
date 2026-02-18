{
  "_comment": "IAM Policy Template — one policy instantiated per tenant at provisioning time.",
  "_variables": {
    "ACCOUNT_ID":  "123456789012",
    "BUCKET_NAME": "rag-platform-documents",
    "TENANT_ID":   "{{TENANT_ID}}",
    "KMS_KEY_ARN": "{{KMS_KEY_ARN}}"
  },

  "Version": "2012-10-17",
  "Statement": [

    {
      "Sid": "AllowTenantPrefixReadWrite",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:GetObjectAttributes",
        "s3:GetObjectTagging",
        "s3:PutObjectTagging"
      ],
      "Resource": "arn:aws:s3:::{{BUCKET_NAME}}/tenants/{{TENANT_ID}}/*",
      "Condition": {
        "StringEquals": {
          "s3:x-amz-server-side-encryption": "aws:kms",
          "s3:x-amz-server-side-encryption-aws-kms-key-id": "{{KMS_KEY_ARN}}"
        }
      }
    },

    {
      "Sid": "AllowTenantPrefixList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::{{BUCKET_NAME}}",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["tenants/{{TENANT_ID}}/*"]
        }
      }
    },

    {
      "Sid": "DenyAllOtherPrefixes",
      "Effect": "Deny",
      "Action": ["s3:*"],
      "Resource": [
        "arn:aws:s3:::{{BUCKET_NAME}}",
        "arn:aws:s3:::{{BUCKET_NAME}}/*"
      ],
      "Condition": {
        "StringNotLike": {
          "s3:prefix":  ["tenants/{{TENANT_ID}}/*"],
          "s3:DataAccessedByPrefix": ["tenants/{{TENANT_ID}}/*"]
        }
      }
    },

    {
      "Sid": "AllowTenantKMSUsage",
      "Effect": "Allow",
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "{{KMS_KEY_ARN}}"
    },

    {
      "Sid": "DenyUnencryptedUploads",
      "Effect": "Deny",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::{{BUCKET_NAME}}/tenants/{{TENANT_ID}}/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "aws:kms"
        }
      }
    }
  ]
}
