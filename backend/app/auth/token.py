"""
JWT Token Verification — OIDC-Compatible

Supports two enterprise auth providers with identical JWT verification logic:

  Provider A: AWS Cognito
    Issuer:   https://cognito-idp.<region>.amazonaws.com/<user_pool_id>
    JWKS URI: <issuer>/.well-known/jwks.json
    Claims:   sub, email, custom:tenant_id, custom:role, cognito:groups

  Provider B: Auth0
    Issuer:   https://<tenant>.auth0.com/
    JWKS URI: <issuer>/.well-known/jwks.json
    Claims:   sub, email, https://<api>/tenant_id, https://<api>/role

Both providers sign tokens with RS256 using rotating key sets.
We fetch the public JWKS once and cache it (TTL: 1 hour). If a kid is
missing we force-refresh — handles key rotation transparently.

RBAC roles (embedded in JWT, enforced here and in route dependencies):
  owner   - full tenant admin
  admin   - manage users, upload docs
  member  - query + upload docs
  viewer  - query only
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwk, jwt
from jose.utils import base64url_decode
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP Bearer extractor
# ---------------------------------------------------------------------------

bearer_scheme = HTTPBearer(auto_error=True)


# ---------------------------------------------------------------------------
# Verified token payload
# ---------------------------------------------------------------------------

class TokenPayload(BaseModel):
    """Parsed, validated JWT claims — passed to route handlers."""
    sub:       str          # provider user ID
    email:     str
    tenant_id: UUID
    role:      str          # owner | admin | member | viewer
    exp:       int
    iss:       str


# ---------------------------------------------------------------------------
# JWKS cache (in-memory, TTL-based)
# ---------------------------------------------------------------------------

_JWKS_CACHE: dict[str, tuple[dict, float]] = {}   # issuer → (jwks, fetched_at)
_JWKS_TTL   = 3600   # 1 hour


async def _fetch_jwks(issuer: str) -> dict:
    """Fetch JWKS from the provider's well-known endpoint with TTL caching."""
    now = time.monotonic()
    cached = _JWKS_CACHE.get(issuer)
    if cached and (now - cached[1]) < _JWKS_TTL:
        return cached[0]

    jwks_uri = f"{issuer.rstrip('/')}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        jwks = resp.json()

    _JWKS_CACHE[issuer] = (jwks, now)
    logger.debug("JWKS refreshed for issuer: %s", issuer)
    return jwks


async def _get_signing_key(token: str) -> str:
    """
    Extract kid from token header, fetch matching public key from JWKS.
    Force-refreshes the cache if the kid is not found (handles key rotation).
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token header") from exc

    kid = header.get("kid")
    issuer = settings.auth_issuer

    for attempt in range(2):   # 0 = cached, 1 = force refresh
        if attempt == 1:
            _JWKS_CACHE.pop(issuer, None)   # force refresh

        jwks = await _fetch_jwks(issuer)
        for key_data in jwks.get("keys", []):
            if key_data.get("kid") == kid:
                return jwk.construct(key_data).public_key()

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail=f"Unable to find signing key for kid={kid}",
    )


# ---------------------------------------------------------------------------
# Claim extractors (Cognito vs Auth0 have different claim names)
# ---------------------------------------------------------------------------

def _extract_tenant_id(claims: dict) -> UUID:
    """
    Extract tenant_id from JWT claims.
    Cognito: custom:tenant_id
    Auth0:   https://<api_namespace>/tenant_id
    """
    raw = (
        claims.get("custom:tenant_id")
        or claims.get(f"{settings.auth0_namespace}/tenant_id")
        or claims.get("tenant_id")
    )
    if not raw:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant_id claim",
        )
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid tenant_id in token: {raw}",
        )


def _extract_role(claims: dict) -> str:
    """
    Extract RBAC role from JWT claims.
    Cognito: custom:role  OR  cognito:groups[0]
    Auth0:   https://<api_namespace>/role
    """
    role = (
        claims.get("custom:role")
        or claims.get(f"{settings.auth0_namespace}/role")
        or claims.get("role")
    )
    if not role and "cognito:groups" in claims:
        groups = claims["cognito:groups"]
        role = groups[0] if groups else None

    valid_roles = {"owner", "admin", "member", "viewer"}
    if role not in valid_roles:
        logger.warning("Unknown role '%s' in token, defaulting to 'viewer'", role)
        role = "viewer"

    return role


# ---------------------------------------------------------------------------
# Main verification function
# ---------------------------------------------------------------------------

async def verify_token(token: str) -> TokenPayload:
    """
    Verify a JWT token:
      1. Fetch matching public key from JWKS (cached).
      2. Verify signature, expiry, issuer, audience.
      3. Extract and validate tenant_id + role claims.
      4. Return a typed TokenPayload.
    """
    signing_key = await _get_signing_key(token)

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.auth_audience,
            issuer=settings.auth_issuer,
            options={"verify_exp": True},
        )
    except ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}")

    return TokenPayload(
        sub=claims["sub"],
        email=claims.get("email", ""),
        tenant_id=_extract_tenant_id(claims),
        role=_extract_role(claims),
        exp=claims["exp"],
        iss=claims["iss"],
    )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> TokenPayload:
    """
    FastAPI dependency that extracts and validates the Bearer token.
    Inject into any route that requires authentication:

        @router.get("/documents")
        async def list_docs(user: TokenPayload = Depends(get_current_user)):
            ...
    """
    return await verify_token(credentials.credentials)
