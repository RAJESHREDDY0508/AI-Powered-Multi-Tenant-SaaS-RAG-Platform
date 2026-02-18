"""
Tenant Storage Provisioner

Runs ONCE when a new tenant is created. Provisions:
  1. A dedicated KMS Customer Managed Key (CMK) for the tenant.
  2. An IAM Role scoped strictly to the tenant's S3 prefix + KMS key.
  3. An IAM Policy attached to the role (from the JSON template).

This is called by the tenant-creation admin flow (never by the API layer).
All resources are tagged with tenant_id for cost allocation and audit.

Security notes:
  - Each CMK is single-tenant. Compromise of one key never affects others.
  - IAM role has no permissions outside tenants/<tenant_id>/* prefix.
  - KMS key policy has an explicit Deny for all other principals.
  - All AWS resources are tagged: {ManagedBy: rag-platform, TenantId: <id>}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID

import aioboto3

from app.core.config import settings

logger = logging.getLogger(__name__)

# Path to the IAM / KMS JSON templates in infra/iam/
_TEMPLATE_DIR = Path(__file__).parents[3] / "infra" / "iam"


def _render_template(template_name: str, **kwargs: str) -> str:
    """Load a .json.tpl file and substitute {{VARIABLE}} placeholders."""
    tpl = (_TEMPLATE_DIR / template_name).read_text()
    for key, value in kwargs.items():
        tpl = tpl.replace(f"{{{{{key}}}}}", value)
    return tpl


class TenantStorageProvisioner:
    """
    Provisions AWS resources for a new tenant.
    Should be called from an admin-only endpoint or a background task,
    never from the public API.
    """

    def __init__(self) -> None:
        self._session = aioboto3.Session()

    async def provision(self, tenant_id: UUID, tenant_slug: str) -> dict:
        """
        Full provisioning flow for a new tenant.

        Returns a dict with the provisioned ARNs — store these in the
        tenant record in PostgreSQL (tenants.storage_config JSONB column,
        added in a future migration).

        Steps:
          1. Create KMS key with tenant-scoped key policy.
          2. Create an alias for human readability.
          3. Create IAM role for the tenant service account.
          4. Attach inline IAM policy scoped to tenant prefix + KMS key.
        """
        logger.info("Provisioning storage for tenant %s (%s)", tenant_id, tenant_slug)

        kms_key_arn = await self._create_kms_key(tenant_id, tenant_slug)
        role_arn    = await self._create_iam_role(tenant_id, tenant_slug, kms_key_arn)

        result = {
            "tenant_id":   str(tenant_id),
            "kms_key_arn": kms_key_arn,
            "iam_role_arn": role_arn,
            "s3_prefix":   f"tenants/{tenant_id}/",
            "bucket":      settings.s3_bucket,
        }
        logger.info("Storage provisioned for tenant %s: %s", tenant_id, result)
        return result

    # ------------------------------------------------------------------
    # Step 1: KMS key
    # ------------------------------------------------------------------

    async def _create_kms_key(self, tenant_id: UUID, tenant_slug: str) -> str:
        key_policy = _render_template(
            "kms_key_policy.json.tpl",
            ACCOUNT_ID=settings.aws_account_id,
            TENANT_ID=str(tenant_id),
            AWS_REGION=settings.aws_region,
        )

        async with self._session.client("kms", region_name=settings.aws_region) as kms:
            resp = await kms.create_key(
                Description=f"RAG Platform — Tenant {tenant_slug} ({tenant_id})",
                KeyUsage="ENCRYPT_DECRYPT",
                KeySpec="SYMMETRIC_DEFAULT",
                Policy=key_policy,
                Tags=[
                    {"TagKey": "ManagedBy",  "TagValue": "rag-platform"},
                    {"TagKey": "TenantId",   "TagValue": str(tenant_id)},
                    {"TagKey": "TenantSlug", "TagValue": tenant_slug},
                ],
            )
            key_arn = resp["KeyMetadata"]["Arn"]

            # Friendly alias: alias/rag-tenant-<slug>
            await kms.create_alias(
                AliasName=f"alias/rag-tenant-{tenant_slug}",
                TargetKeyId=key_arn,
            )

        logger.info("KMS key created: %s", key_arn)
        return key_arn

    # ------------------------------------------------------------------
    # Step 2: IAM role + inline policy
    # ------------------------------------------------------------------

    async def _create_iam_role(
        self, tenant_id: UUID, tenant_slug: str, kms_key_arn: str
    ) -> str:
        role_name = f"rag-tenant-{tenant_slug}"

        # Trust policy: allows ECS tasks / Lambda to assume this role
        trust_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": settings.aws_account_id
                    }
                },
            }],
        })

        # Inline policy from template
        inline_policy = _render_template(
            "tenant_iam_policy.json.tpl",
            TENANT_ID=str(tenant_id),
            KMS_KEY_ARN=kms_key_arn,
            BUCKET_NAME=settings.s3_bucket,
            ACCOUNT_ID=settings.aws_account_id,
        )

        async with self._session.client("iam") as iam:
            # Create role
            resp = await iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=trust_policy,
                Description=f"RAG Platform service role for tenant {tenant_slug}",
                Tags=[
                    {"Key": "ManagedBy",  "Value": "rag-platform"},
                    {"Key": "TenantId",   "Value": str(tenant_id)},
                    {"Key": "TenantSlug", "Value": tenant_slug},
                ],
            )
            role_arn = resp["Role"]["Arn"]

            # Attach inline policy (not managed — tightly coupled to this tenant)
            await iam.put_role_policy(
                RoleName=role_name,
                PolicyName=f"rag-tenant-{tenant_slug}-s3-kms",
                PolicyDocument=inline_policy,
            )

        logger.info("IAM role created: %s", role_arn)
        return role_arn

    # ------------------------------------------------------------------
    # Teardown (called when a tenant is deleted / offboarded)
    # ------------------------------------------------------------------

    async def deprovision(self, tenant_id: UUID, tenant_slug: str, kms_key_arn: str) -> None:
        """
        Remove AWS resources for an offboarded tenant.
        Order matters: detach policy → delete role → schedule key deletion.
        """
        logger.warning("Deprovisioning tenant %s — THIS IS IRREVERSIBLE", tenant_id)

        role_name   = f"rag-tenant-{tenant_slug}"
        policy_name = f"rag-tenant-{tenant_slug}-s3-kms"

        async with self._session.client("iam") as iam:
            try:
                await iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
                await iam.delete_role(RoleName=role_name)
                logger.info("IAM role deleted: %s", role_name)
            except iam.exceptions.NoSuchEntityException:
                logger.warning("IAM role not found (already deleted?): %s", role_name)

        async with self._session.client("kms", region_name=settings.aws_region) as kms:
            try:
                # Schedule key deletion — minimum 7 days window (AWS enforced)
                await kms.schedule_key_deletion(
                    KeyId=kms_key_arn,
                    PendingWindowInDays=7,
                )
                logger.info("KMS key scheduled for deletion: %s", kms_key_arn)
            except Exception as exc:
                logger.error("Failed to schedule KMS key deletion: %s", exc)
