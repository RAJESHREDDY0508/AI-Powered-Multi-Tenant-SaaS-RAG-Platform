{
  "_comment": "KMS Key Policy — one key created per tenant. Key is dedicated and cannot be used by other tenants.",

  "Version": "2012-10-17",
  "Statement": [

    {
      "Sid": "EnableRootAccountAdmin",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::{{ACCOUNT_ID}}:root"
      },
      "Action": "kms:*",
      "Resource": "*"
    },

    {
      "Sid": "AllowTenantServiceRoleUsage",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::{{ACCOUNT_ID}}:role/rag-tenant-{{TENANT_ID}}"
      },
      "Action": [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey"
      ],
      "Resource": "*"
    },

    {
      "Sid": "AllowS3ServiceUsage",
      "Effect": "Allow",
      "Principal": {
        "Service": "s3.amazonaws.com"
      },
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "s3.{{AWS_REGION}}.amazonaws.com",
          "kms:CallerAccount": "{{ACCOUNT_ID}}"
        }
      }
    },

    {
      "Sid": "DenyAllOtherPrincipals",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "kms:*",
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:PrincipalArn": [
            "arn:aws:iam::{{ACCOUNT_ID}}:root",
            "arn:aws:iam::{{ACCOUNT_ID}}:role/rag-tenant-{{TENANT_ID}}"
          ]
        }
      }
    }
  ]
}
