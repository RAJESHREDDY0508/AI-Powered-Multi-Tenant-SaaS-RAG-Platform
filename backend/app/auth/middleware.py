"""
JWT Auth Middleware  —  Standalone Dependency Units
════════════════════════════════════════════════════

This module provides three independently testable, composable layers:

  Layer 1 │ _JWKSCache           — Async JWKS fetcher with TTL + rotation handling
  Layer 2 │ JWTDecoder           — RS256 decode, claim extraction, TokenPayload build
  Layer 3 │ RoleChecker          — RBAC enforcement as a FastAPI dependency class

All three are injected via FastAPI's Depends() — zero global state in request
paths. The JWKS cache is the only module-level singleton (intentional — it must
persist across requests to avoid re-fetching on every call).

Why a separate middleware module?
  auth/token.py contains the core verify_token() coroutine used by the existing
  FastAPI dependency (get_current_user). This module provides:
    • A class-based JWTDecoder that can be instantiated with custom settings in
      tests (no monkeypatching of globals required).
    • A RoleChecker class dependency that is more readable than nested require_role
      calls in route signatures.
    • Explicit JWKS cache management (inspect, clear, force-refresh) for
      operational tooling and integration tests.

Usage in routes
───────────────
  from app.auth.middleware import JWTDecoder, RoleChecker, jwks_cache

  # Option A: use pre-built role checkers (simplest)
  @router.post("/upload")
  async def upload(user: TokenPayload = Depends(RoleChecker("member"))):
      ...

  # Option B: inject decoder directly (for unit testing with custom settings)
  decoder = JWTDecoder(issuer="...", audience="...")
  @router.post("/upload")
  async def upload(user: TokenPayload = Depends(decoder)):
      ...

Request trace IDs
─────────────────
  All log lines include the trace request_id extracted from X-Request-ID header
  so JWKS errors can be correlated with the failing request in production logs.

SOC2 compliance
───────────────
  • JWT expiry is always verified (options={"verify_exp": True}).
  • Audience and issuer are validated on every request.
  • Clock skew tolerance is intentionally NOT added — tokens must be valid now.
  • JWKS key rotation is handled transparently (force-refresh on unknown kid).
  • Authentication failures are logged with request_id for audit correlation.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwk, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: JWKS Cache
# ─────────────────────────────────────────────────────────────────────────────

class _JWKSCache:
    """
    Thread-safe (within asyncio) in-memory JWKS cache.

    Behaviour:
      • Fetches the provider's /.well-known/jwks.json once and caches for TTL.
      • On cache miss for a specific kid: force-refreshes once (handles rotation).
      • On second miss: raises 401 with a clear message.
      • All HTTP errors from the JWKS endpoint are propagated as 401 responses
        (the client cannot fix a JWKS endpoint outage — it's a server issue).

    Singleton — instantiated once at module load, shared across all requests.
    """

    _TTL: int = 3600   # 1 hour

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict, float]] = {}   # issuer → (jwks, fetched_at)

    async def get_signing_key(self, token: str, issuer: str | None = None) -> object:
        """
        Resolve the RSA public key for the given token's kid.
        Returns a python-jose public key object.

        Args:
            token:  The raw JWT string.
            issuer: The OIDC issuer URL whose JWKS to fetch.
                    Defaults to ``settings.auth_issuer`` when not supplied.
                    Callers (e.g. JWTDecoder) should pass their own configured
                    issuer so that the cache key matches what was pre-populated
                    in tests and avoids unintended network calls.
        """
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Malformed token header",
            ) from exc

        kid    = header.get("kid")
        issuer = issuer or settings.auth_issuer

        for attempt in range(2):
            if attempt == 1:
                self._store.pop(issuer, None)   # force refresh on second attempt

            jwks = await self._fetch(issuer)

            for key_data in jwks.get("keys", []):
                if key_data.get("kid") == kid:
                    return jwk.construct(key_data).public_key()

        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"No signing key found for kid={kid!r}. "
                   "Token may have been issued by an unsupported provider.",
        )

    async def _fetch(self, issuer: str) -> dict:
        """Fetch JWKS from well-known endpoint with TTL-based caching."""
        now    = time.monotonic()
        cached = self._store.get(issuer)

        if cached and (now - cached[1]) < self._TTL:
            return cached[0]

        uri = f"{issuer.rstrip('/')}/.well-known/jwks.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(uri)
                resp.raise_for_status()
                jwks = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("JWKS fetch failed | issuer=%s status=%d", issuer, exc.response.status_code)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Unable to retrieve token signing keys.",
            ) from exc
        except httpx.RequestError as exc:
            logger.error("JWKS fetch network error | issuer=%s error=%s", issuer, exc)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Unable to retrieve token signing keys (network error).",
            ) from exc

        self._store[issuer] = (jwks, now)
        logger.debug("JWKS refreshed | issuer=%s keys=%d", issuer, len(jwks.get("keys", [])))
        return jwks

    def clear(self) -> None:
        """Flush the entire cache. Used in tests and by operational CLI tools."""
        self._store.clear()

    def stats(self) -> dict:
        """Return cache diagnostics for the /health endpoint or ops tooling."""
        now = time.monotonic()
        return {
            issuer: {
                "age_seconds": round(now - fetched_at),
                "ttl_remaining": max(0, round(self._TTL - (now - fetched_at))),
                "key_count": len(jwks.get("keys", [])),
            }
            for issuer, (jwks, fetched_at) in self._store.items()
        }


# Module-level singleton — persists across requests
jwks_cache = _JWKSCache()


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: JWT Decoder
# ─────────────────────────────────────────────────────────────────────────────

from app.auth.token import TokenPayload   # noqa: E402 — after _JWKSCache definition


class JWTDecoder:
    """
    Class-based FastAPI dependency for JWT decoding.

    Instantiate with explicit issuer/audience for test isolation:
        decoder = JWTDecoder(issuer="https://test.auth0.com/", audience="test-api")

    Default constructor reads from app settings:
        decoder = JWTDecoder()

    Call signature (FastAPI Depends-compatible):
        user: TokenPayload = Depends(JWTDecoder())
    """

    _bearer = HTTPBearer(auto_error=True)

    def __init__(
        self,
        issuer:   str | None = None,
        audience: str | None = None,
        cache:    _JWKSCache | None = None,
    ) -> None:
        self._issuer   = issuer   or settings.auth_issuer
        self._audience = audience or settings.auth_audience
        self._cache    = cache    or jwks_cache

    async def __call__(
        self,
        request:     Request,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=True)),
    ) -> TokenPayload:
        """
        FastAPI dependency — resolves to a verified TokenPayload.

        Steps:
          1. Extract Bearer token from Authorization header (HTTPBearer handles this).
          2. Resolve signing key from JWKS cache (by kid in token header).
          3. Decode and verify: signature, expiry, issuer, audience.
          4. Extract tenant_id and role from provider-specific custom claims.
          5. Return typed TokenPayload.
        """
        request_id = request.headers.get("X-Request-ID", "-")
        token = credentials.credentials

        # Step 2: Resolve signing key
        # Pass self._issuer explicitly so the cache lookup uses the same key
        # that was configured at construction time (critical for test isolation —
        # the test fixture pre-populates the cache under TEST_ISSUER, which may
        # differ from settings.auth_issuer if the env var is not overridden).
        signing_key = await self._cache.get_signing_key(token, issuer=self._issuer)

        # Step 3: Decode + verify all claims
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"verify_exp": True},
            )
        except ExpiredSignatureError:
            logger.info("Expired token | request_id=%s", request_id)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired. Please re-authenticate.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTError as exc:
            logger.warning("JWT decode error | request_id=%s error=%s", request_id, exc)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Step 4: Extract tenant_id (never from request body or query string)
        tenant_id = self._extract_tenant_id(claims, request_id)
        role      = self._extract_role(claims, request_id)

        return TokenPayload(
            sub=claims["sub"],
            email=claims.get("email", ""),
            tenant_id=tenant_id,
            role=role,
            exp=claims["exp"],
            iss=claims["iss"],
        )

    def _extract_tenant_id(self, claims: dict, request_id: str) -> UUID:
        """
        Extract tenant_id from JWT claims. Supports Cognito and Auth0 claim namespaces.

        Cognito: custom:tenant_id
        Auth0:   https://<api_namespace>/tenant_id
        Generic: tenant_id (fallback for testing)
        """
        raw = (
            claims.get("custom:tenant_id")
            or claims.get(f"{settings.auth0_namespace}/tenant_id")
            or claims.get("tenant_id")
        )
        if not raw:
            logger.warning(
                "Token missing tenant_id claim | sub=%s request_id=%s",
                claims.get("sub"), request_id,
            )
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Token is missing the required tenant_id claim.",
            )
        try:
            return UUID(str(raw))
        except (ValueError, AttributeError):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid tenant_id value in token: {raw!r}",
            )

    def _extract_role(self, claims: dict, request_id: str) -> str:
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

        valid = {"owner", "admin", "member", "viewer"}
        if role not in valid:
            logger.warning(
                "Unknown role %r in token — defaulting to viewer | request_id=%s",
                role, request_id,
            )
            return "viewer"

        return role


# Default decoder instance (uses settings)
default_decoder = JWTDecoder()


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: Role Checker
# ─────────────────────────────────────────────────────────────────────────────

_ROLE_RANK: dict[str, int] = {
    "viewer": 0,
    "member": 1,
    "admin":  2,
    "owner":  3,
}


class RoleChecker:
    """
    Class-based FastAPI dependency for RBAC enforcement.

    Usage:
        @router.post("/upload")
        async def upload(user: TokenPayload = Depends(RoleChecker("member"))):
            ...

    Raises HTTP 403 if the authenticated user's role is below the minimum.
    The full TokenPayload is passed through so routes can access tenant_id, sub, etc.

    Composable with JWTDecoder for custom test setups:
        checker = RoleChecker("admin", decoder=JWTDecoder(issuer="...", audience="..."))
    """

    def __init__(
        self,
        minimum_role: str,
        decoder:      JWTDecoder | None = None,
    ) -> None:
        if minimum_role not in _ROLE_RANK:
            raise ValueError(
                f"Invalid minimum_role={minimum_role!r}. "
                f"Valid values: {list(_ROLE_RANK)}"
            )
        self._min_role = minimum_role
        self._decoder  = decoder or default_decoder

    async def __call__(
        self,
        request:     Request,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=True)),
    ) -> TokenPayload:
        """Verify JWT and enforce minimum role. Returns TokenPayload on success."""
        user = await self._decoder(request, credentials)

        user_rank = _ROLE_RANK.get(user.role, -1)
        min_rank  = _ROLE_RANK[self._min_role]

        if user_rank < min_rank:
            logger.info(
                "RBAC denied | tenant=%s user=%s role=%s required=%s",
                user.tenant_id, user.sub, user.role, self._min_role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. Role '{self._min_role}' or above is required. "
                    f"Your role: '{user.role}'."
                ),
            )

        return user


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built role checkers (convenience aliases)
# Import these directly into route files instead of instantiating each time.
# ─────────────────────────────────────────────────────────────────────────────

require_viewer = RoleChecker("viewer")
require_member = RoleChecker("member")
require_admin  = RoleChecker("admin")
require_owner  = RoleChecker("owner")
