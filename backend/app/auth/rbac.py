"""
Role-Based Access Control (RBAC)

Role hierarchy (highest → lowest privilege):
    owner > admin > member > viewer

Every FastAPI route that mutates state or accesses sensitive data
must declare its minimum required role using the require_role() dependency.

Usage:
    @router.delete("/documents/{doc_id}")
    async def delete_doc(
        doc_id: UUID,
        user: TokenPayload = Depends(require_role("admin")),  # admin or above
    ): ...

The dependency raises 403 if the user's role is below the requirement.
It passes through the full TokenPayload so routes can also access
tenant_id, sub, email, etc.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.auth.token import TokenPayload, get_current_user

# ---------------------------------------------------------------------------
# Role ordering — higher index = more privilege
# ---------------------------------------------------------------------------

_ROLE_ORDER: dict[str, int] = {
    "viewer":  0,
    "member":  1,
    "admin":   2,
    "owner":   3,
}


def _has_role(user_role: str, required_role: str) -> bool:
    """Return True if user_role meets or exceeds required_role."""
    user_level     = _ROLE_ORDER.get(user_role, -1)
    required_level = _ROLE_ORDER.get(required_role, 999)
    return user_level >= required_level


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------

def require_role(minimum_role: str):
    """
    Returns a FastAPI dependency that:
      1. Verifies the JWT (via get_current_user).
      2. Checks the user's role meets the minimum requirement.
      3. Passes the TokenPayload to the route handler.

    Args:
        minimum_role: Minimum role required — "viewer" | "member" | "admin" | "owner"
    """
    async def _dependency(
        user: Annotated[TokenPayload, Depends(get_current_user)],
    ) -> TokenPayload:
        if not _has_role(user.role, minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Insufficient permissions. "
                    f"Required: '{minimum_role}', your role: '{user.role}'."
                ),
            )
        return user

    return _dependency


# ---------------------------------------------------------------------------
# Convenience aliases for common roles
# ---------------------------------------------------------------------------

RequireViewer = Depends(require_role("viewer"))
RequireMember = Depends(require_role("member"))
RequireAdmin  = Depends(require_role("admin"))
RequireOwner  = Depends(require_role("owner"))
