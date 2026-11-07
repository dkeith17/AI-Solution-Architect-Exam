"""Bearer-token authentication: HS256-signed JWT carrying a ``role`` claim.

Hardening choices:
* ``algorithms=["HS256"]`` is pinned, so ``alg: none`` and algorithm-confusion
  tokens are rejected.
* ``exp``, ``iat``, ``sub`` and ``role`` are required claims; ``iss`` and ``aud``
  are verified against configuration.
* The role must be one of ``KNOWN_ROLES``; anything else fails closed.
* The secret must be at least 32 bytes (RFC 7518 section 3.2 requires an HS256 key
  of at least the hash output size, 256 bits).
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

KNOWN_ROLES = frozenset({"admin", "viewer"})
MIN_SECRET_BYTES = 32
CLOCK_SKEW_SECONDS = 30


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str


class AuthError(Exception):
    """Authentication failed. ``reason`` is a short, non-sensitive code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AuthSettings:
    secret: str
    issuer: str
    audience: str

    def __post_init__(self) -> None:
        if len(self.secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise ValueError(f"JWT secret must be at least {MIN_SECRET_BYTES} bytes")


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("missing_token")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise AuthError("malformed_authorization_header")
    return token


def authenticate(authorization: str | None, settings: AuthSettings) -> Principal:
    token = extract_bearer(authorization)
    try:
        claims = jwt.decode(
            token,
            settings.secret,
            algorithms=["HS256"],
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "sub", "role"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("token_expired") from None
    except jwt.InvalidTokenError:
        raise AuthError("invalid_token") from None

    role = claims.get("role")
    sub = claims.get("sub")
    if not isinstance(role, str) or role not in KNOWN_ROLES:
        raise AuthError("invalid_role")
    if not isinstance(sub, str) or not sub:
        raise AuthError("invalid_subject")
    return Principal(subject=sub, role=role)


def mint_token(settings: AuthSettings, *, subject: str, role: str, ttl_seconds: int, now: int) -> str:
    """Create a signed token (used by the CLI and the tests)."""
    if role not in KNOWN_ROLES:
        raise ValueError(f"role must be one of {sorted(KNOWN_ROLES)}")
    payload = {
        "sub": subject,
        "role": role,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, settings.secret, algorithm="HS256")
