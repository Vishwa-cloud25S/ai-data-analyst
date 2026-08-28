"""API-key authentication and role-based authorisation.

Deliberately simple and self-contained: no external identity provider, no JWT
infrastructure to operate. Keys are supplied via environment configuration and
stored only as SHA-256 digests in memory, compared in constant time.

Roles
-----
viewer   ask questions
analyst  viewer + inspect the validator (/validate-sql)
admin    analyst + read the audit log

Configuration
-------------
    API_KEYS="key1:admin:alice,key2:analyst:bob,key3:viewer:dashboard"

Fail-closed: if AUTH_ENABLED is true and no keys are configured, the app
refuses to start rather than silently serving an open endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import settings

log = logging.getLogger(__name__)

ROLES = ("viewer", "analyst", "admin")
ROLE_RANK = {"viewer": 0, "analyst": 1, "admin": 2}

ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller (or the anonymous one when auth is disabled)."""

    name: str
    role: str

    @property
    def authenticated(self) -> bool:
        return self.name != ANONYMOUS

    def can(self, required: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[required]


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyRing:
    """Digest -> Principal. Plaintext keys are never retained."""

    def __init__(self, spec: str = ""):
        self._keys: dict[str, Principal] = {}
        for entry in (spec or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                log.warning("Ignoring malformed API_KEYS entry (expected key:role[:name])")
                continue
            raw, role = parts[0].strip(), parts[1].strip().lower()
            name = parts[2].strip() if len(parts) > 2 else role
            if role not in ROLES:
                log.warning("Ignoring API key with unknown role %r", role)
                continue
            if len(raw) < 16:
                log.warning("Ignoring API key for %r: keys must be at least 16 characters", name)
                continue
            self._keys[_digest(raw)] = Principal(name=name, role=role)

    def __len__(self) -> int:
        return len(self._keys)

    def resolve(self, presented: str) -> Principal | None:
        """Constant-time lookup: compare against every digest, no early exit."""
        target = _digest(presented)
        found: Principal | None = None
        for digest, principal in self._keys.items():
            if hmac.compare_digest(digest, target):
                found = principal
        return found

    def describe(self) -> list[dict[str, str]]:
        return sorted(
            ({"name": p.name, "role": p.role} for p in self._keys.values()),
            key=lambda d: d["name"],
        )


_keyring: KeyRing | None = None


def get_keyring() -> KeyRing:
    global _keyring
    if _keyring is None:
        _keyring = KeyRing(settings.api_keys)
    return _keyring


def reset_keyring() -> None:
    """Test hook: rebuild the keyring from current settings."""
    global _keyring
    _keyring = None


def verify_startup_config() -> None:
    """Fail closed: auth on with no keys is a misconfiguration, not a default."""
    if settings.auth_enabled and len(get_keyring()) == 0:
        raise RuntimeError(
            "AUTH_ENABLED is true but API_KEYS is empty. Refusing to start an "
            "unauthenticated API. Set API_KEYS='<key>:admin:<name>' or set "
            "AUTH_ENABLED=false for local development."
        )


def current_principal(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    """Resolve the caller.

    With auth disabled the caller is anonymous with `ANONYMOUS_ROLE` (analyst by
    default). It is deliberately not admin: an unauthenticated deployment must
    not expose the audit log or the semantic-layer editor to whoever finds the
    URL.
    """
    if not settings.auth_enabled:
        return Principal(name=ANONYMOUS, role=settings.anonymous_role)

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    principal = get_keyring().resolve(x_api_key)
    if principal is None:
        log.warning("Rejected API key from %s", request.client.host if request.client else "?")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return principal


def require_authenticated(role: str):
    """Like `require`, but never satisfied by an anonymous caller.

    For endpoints that change what the system can reach. Even an operator who
    sets ANONYMOUS_ROLE=admin for local convenience should not be able to
    mutate the semantic layer without presenting a key.
    """

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.authenticated:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This endpoint changes what the assistant can access and "
                       "requires an authenticated admin key. Set AUTH_ENABLED=true "
                       "and API_KEYS, then send X-API-Key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if not principal.can(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires the '{role}' role; "
                       f"'{principal.name}' has '{principal.role}'.",
            )
        return principal

    return _dep


def require(role: str):
    """Dependency factory enforcing a minimum role."""

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires the '{role}' role; "
                       f"'{principal.name}' has '{principal.role}'.",
            )
        return principal

    return _dep
