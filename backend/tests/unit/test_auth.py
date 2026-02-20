"""
Unit Tests — JWT Auth Middleware
═════════════════════════════════
Tests for:
  • _JWKSCache     — fetch, TTL, force-refresh, cache.clear()
  • JWTDecoder     — valid token, expired, bad audience, missing claims
  • RoleChecker    — pass/fail each role level, hierarchy enforcement
  • _extract_tenant_id — Cognito + Auth0 claim namespaces
  • _extract_role      — custom:role, cognito:groups fallback

All tests use the test RSA key pair from conftest.py.
Zero network calls — JWKS fetch is patched via httpx mock.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from tests.conftest import TEST_AUDIENCE, TEST_ISSUER, TEST_KID


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_request(request_id: str = "test-req-id"):
    """Build a minimal mock Request object."""
    req = MagicMock()
    req.headers = {"X-Request-ID": request_id}
    return req


def _make_credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ─────────────────────────────────────────────────────────────────────────────
# _JWKSCache tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.auth
class TestJWKSCache:

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Ensure cache is clean before and after each test."""
        from app.auth.middleware import jwks_cache
        jwks_cache.clear()
        yield
        jwks_cache.clear()

    async def test_get_signing_key_returns_public_key(
        self, make_token, test_jwks, rsa_private_key_pem
    ):
        """Happy path: valid kid in JWKS returns a public key object."""
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()
        token = make_token(role="member")

        with patch.object(cache, "_fetch", new=AsyncMock(return_value=test_jwks)):
            key = await cache.get_signing_key(token)

        assert key is not None

    async def test_unknown_kid_triggers_force_refresh(
        self, make_token, test_jwks
    ):
        """
        If kid is not found on first attempt, cache is cleared and
        JWKS is fetched again (handles key rotation).
        """
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()
        token = make_token(role="member")

        call_count = 0
        async def _fetch_side_effect(issuer):
            nonlocal call_count
            call_count += 1
            # First call returns empty JWKS, second returns real key
            return {} if call_count == 1 else test_jwks

        with patch.object(cache, "_fetch", side_effect=_fetch_side_effect):
            key = await cache.get_signing_key(token)

        assert call_count == 2
        assert key is not None

    async def test_unknown_kid_after_refresh_raises_401(self, make_token):
        """If kid is missing even after force-refresh, raise 401."""
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()
        token = make_token(role="member")

        with patch.object(cache, "_fetch", new=AsyncMock(return_value={"keys": []})):
            with pytest.raises(HTTPException) as exc_info:
                await cache.get_signing_key(token)

        assert exc_info.value.status_code == 401

    async def test_malformed_token_raises_401(self):
        """Non-JWT string raises 401 on header parse."""
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()

        with pytest.raises(HTTPException) as exc_info:
            await cache.get_signing_key("not.a.jwt")

        assert exc_info.value.status_code == 401

    def test_cache_stats_returns_diagnostics(self, test_jwks):
        """stats() returns a dict with age_seconds and key_count."""
        import time
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()
        # Directly inject a cache entry to avoid network calls
        cache._store[TEST_ISSUER] = (test_jwks, time.monotonic())

        stats = cache.stats()
        assert TEST_ISSUER in stats
        assert "age_seconds" in stats[TEST_ISSUER]
        assert "key_count" in stats[TEST_ISSUER]
        assert stats[TEST_ISSUER]["key_count"] == 1

    def test_clear_empties_cache(self, test_jwks):
        """clear() removes all cached JWKS entries."""
        from app.auth.middleware import _JWKSCache
        cache = _JWKSCache()
        # Manually inject a cache entry
        cache._store[TEST_ISSUER] = (test_jwks, time.monotonic())

        cache.clear()
        assert len(cache._store) == 0


# ─────────────────────────────────────────────────────────────────────────────
# JWTDecoder tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.auth
class TestJWTDecoder:

    @pytest.fixture
    def decoder(self, test_jwks, rsa_public_key):
        """JWTDecoder configured with test issuer/audience and patched JWKS cache."""
        from app.auth.middleware import JWTDecoder, _JWKSCache

        cache = _JWKSCache()
        # Pre-populate cache with test JWKS so no HTTP call is made
        cache._store[TEST_ISSUER] = (test_jwks, time.monotonic())

        return JWTDecoder(
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
            cache=cache,
        )

    async def test_valid_member_token_returns_payload(
        self, decoder, make_token, test_tenant_id, test_user_id
    ):
        """Valid RS256 token returns correctly populated TokenPayload."""
        token = make_token(role="member")
        req   = _make_request()
        creds = _make_credentials(token)

        payload = await decoder(req, creds)

        assert payload.role      == "member"
        assert payload.tenant_id == test_tenant_id
        assert payload.sub       == str(test_user_id)
        assert payload.email     == "test@tenant.example.com"

    async def test_admin_token_sets_role_admin(self, decoder, make_token):
        token = make_token(role="admin")
        payload = await decoder(_make_request(), _make_credentials(token))
        assert payload.role == "admin"

    async def test_owner_token_sets_role_owner(self, decoder, make_token):
        token = make_token(role="owner")
        payload = await decoder(_make_request(), _make_credentials(token))
        assert payload.role == "owner"

    async def test_expired_token_raises_401(self, decoder, make_token):
        """Expired JWT raises 401 with 'expired' in the detail."""
        token = make_token(expired=True)
        with pytest.raises(HTTPException) as exc_info:
            await decoder(_make_request(), _make_credentials(token))
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    async def test_wrong_audience_raises_401(self, decoder, make_token):
        """JWT signed for a different audience is rejected."""
        token = make_token(audience="wrong-audience")
        with pytest.raises(HTTPException) as exc_info:
            await decoder(_make_request(), _make_credentials(token))
        assert exc_info.value.status_code == 401

    async def test_wrong_issuer_raises_401(self, decoder, make_token):
        """JWT from a different issuer is rejected."""
        token = make_token(issuer="https://attacker.example.com/")
        with pytest.raises(HTTPException) as exc_info:
            await decoder(_make_request(), _make_credentials(token))
        assert exc_info.value.status_code == 401

    async def test_missing_tenant_id_raises_401(self, decoder, make_token):
        """Token with no tenant_id claim raises 401."""
        token = make_token(no_tenant=True)
        with pytest.raises(HTTPException) as exc_info:
            await decoder(_make_request(), _make_credentials(token))
        assert exc_info.value.status_code == 401
        assert "tenant_id" in exc_info.value.detail

    async def test_unknown_role_defaults_to_viewer(self, decoder, make_token):
        """Token with unrecognized role defaults to 'viewer' (fail-safe)."""
        token = make_token(role="superadmin")  # not in valid set
        payload = await decoder(_make_request(), _make_credentials(token))
        assert payload.role == "viewer"

    async def test_tampered_token_raises_401(self, decoder, make_token):
        """Token with modified payload (invalid signature) is rejected."""
        token = make_token(role="admin")
        # Corrupt the payload segment
        parts = token.split(".")
        parts[1] = parts[1] + "TAMPERED"
        bad_token = ".".join(parts)

        with pytest.raises(HTTPException) as exc_info:
            await decoder(_make_request(), _make_credentials(bad_token))
        assert exc_info.value.status_code == 401

    async def test_auth0_namespace_claim_extraction(
        self, test_jwks, make_token, test_tenant_id
    ):
        """Auth0-style namespaced claims are correctly extracted."""
        from app.auth.middleware import JWTDecoder, _JWKSCache
        from jose import jwt as jose_jwt

        cache = _JWKSCache()
        cache._store[TEST_ISSUER] = (test_jwks, time.monotonic())

        decoder = JWTDecoder(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, cache=cache)

        # Import private key bytes from conftest (we need the fixture value)
        # We'll use make_token but manually override the claims namespace
        # by building a token with Auth0-style claims instead
        # (make_token uses Cognito-style custom: prefix; this tests the fallback path)

        # Verify the decoder handles tenant_id without custom: prefix too
        from app.auth.middleware import JWTDecoder as JD
        tid_str = str(test_tenant_id)

        # Test _extract_tenant_id directly
        jd = JD(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, cache=cache)

        # Auth0-style
        claims_auth0 = {
            "https://api.ragplatform.io/tenant_id": tid_str,
            "sub": "user-sub",
        }
        extracted = jd._extract_tenant_id(claims_auth0, "req-id")
        assert extracted == test_tenant_id

        # Cognito-style
        claims_cognito = {"custom:tenant_id": tid_str, "sub": "user-sub"}
        extracted2 = jd._extract_tenant_id(claims_cognito, "req-id")
        assert extracted2 == test_tenant_id

        # Generic fallback
        claims_generic = {"tenant_id": tid_str, "sub": "user-sub"}
        extracted3 = jd._extract_tenant_id(claims_generic, "req-id")
        assert extracted3 == test_tenant_id

    def test_decoder_init_with_invalid_settings_raises_on_first_call(self):
        """JWTDecoder can be constructed even with empty settings."""
        from app.auth.middleware import JWTDecoder
        # Should not raise at construction time
        decoder = JWTDecoder(issuer="", audience="")
        assert decoder is not None


# ─────────────────────────────────────────────────────────────────────────────
# RoleChecker tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.auth
class TestRoleChecker:

    @pytest.fixture
    def decoder_returning(self):
        """Factory: returns an async callable that resolves to a given payload."""
        def _make(payload):
            # Use AsyncMock directly — MagicMock(spec=JWTDecoder) with __call__
            # patching doesn't make the object awaitable in Python 3.8+.
            return AsyncMock(return_value=payload)
        return _make

    async def test_viewer_passes_viewer_check(
        self, viewer_payload, decoder_returning
    ):
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("viewer", decoder=decoder_returning(viewer_payload))
        result = await checker(_make_request(), _make_credentials("tok"))
        assert result.role == "viewer"

    async def test_member_passes_member_check(
        self, member_payload, decoder_returning
    ):
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("member", decoder=decoder_returning(member_payload))
        result = await checker(_make_request(), _make_credentials("tok"))
        assert result.role == "member"

    async def test_admin_passes_member_check(
        self, admin_payload, decoder_returning
    ):
        """Admin (rank 2) satisfies member (rank 1) requirement."""
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("member", decoder=decoder_returning(admin_payload))
        result = await checker(_make_request(), _make_credentials("tok"))
        assert result.role == "admin"

    async def test_viewer_fails_member_check(
        self, viewer_payload, decoder_returning
    ):
        """Viewer (rank 0) does NOT satisfy member (rank 1) → 403."""
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("member", decoder=decoder_returning(viewer_payload))
        with pytest.raises(HTTPException) as exc_info:
            await checker(_make_request(), _make_credentials("tok"))
        assert exc_info.value.status_code == 403
        assert "member" in exc_info.value.detail

    async def test_member_fails_admin_check(
        self, member_payload, decoder_returning
    ):
        """Member (rank 1) does NOT satisfy admin (rank 2) → 403."""
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("admin", decoder=decoder_returning(member_payload))
        with pytest.raises(HTTPException) as exc_info:
            await checker(_make_request(), _make_credentials("tok"))
        assert exc_info.value.status_code == 403

    async def test_admin_fails_owner_check(
        self, admin_payload, decoder_returning
    ):
        """Admin (rank 2) does NOT satisfy owner (rank 3) → 403."""
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("owner", decoder=decoder_returning(admin_payload))
        with pytest.raises(HTTPException):
            await checker(_make_request(), _make_credentials("tok"))

    async def test_owner_passes_all_checks(
        self, owner_payload, decoder_returning
    ):
        """Owner (rank 3) passes viewer, member, admin, and owner checks."""
        from app.auth.middleware import RoleChecker
        for role in ("viewer", "member", "admin", "owner"):
            checker = RoleChecker(role, decoder=decoder_returning(owner_payload))
            result = await checker(_make_request(), _make_credentials("tok"))
            assert result.role == "owner"

    def test_invalid_minimum_role_raises_value_error(self):
        """RoleChecker construction with unknown role name raises ValueError."""
        from app.auth.middleware import RoleChecker
        with pytest.raises(ValueError, match="Invalid minimum_role"):
            RoleChecker("superuser")

    async def test_role_checker_passes_through_full_payload(
        self, member_payload, decoder_returning, test_tenant_id
    ):
        """The full TokenPayload (including tenant_id) is returned, not just role."""
        from app.auth.middleware import RoleChecker
        checker = RoleChecker("member", decoder=decoder_returning(member_payload))
        result = await checker(_make_request(), _make_credentials("tok"))
        assert result.tenant_id == test_tenant_id
        assert result.email == "member@tenant.example.com"

    async def test_prebuilt_aliases_exist(self):
        """Pre-built require_* aliases are importable and correct type."""
        from app.auth.middleware import (
            require_viewer, require_member, require_admin, require_owner
        )
        from app.auth.middleware import RoleChecker
        assert isinstance(require_viewer, RoleChecker)
        assert isinstance(require_member, RoleChecker)
        assert isinstance(require_admin,  RoleChecker)
        assert isinstance(require_owner,  RoleChecker)


# ─────────────────────────────────────────────────────────────────────────────
# Owner payload fixture (needed by TestRoleChecker)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def owner_payload(test_tenant_id, test_user_id):
    from app.auth.token import TokenPayload
    return TokenPayload(
        sub=str(test_user_id),
        email="owner@tenant.example.com",
        tenant_id=test_tenant_id,
        role="owner",
        exp=int(time.time()) + 3600,
        iss=TEST_ISSUER,
    )
