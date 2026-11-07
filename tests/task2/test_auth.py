"""JWT bearer authentication (PyJWT, HS256)."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest

from task2_gateway.auth import AuthError, AuthSettings, authenticate, extract_bearer, mint_token

SECRET = "s" * 64  # 64 bytes: long enough for HS512 too (RFC 7518 section 3.2), so the
                    # wrong-algorithm test signs with this same key without a key-length warning
SETTINGS = AuthSettings(secret=SECRET, issuer="quilr-sa-exam", audience="quilr-mcp-gateway")


def token(role: str = "viewer", **over) -> str:
    now = int(time.time())
    payload = {"sub": "alice", "role": role, "iss": SETTINGS.issuer, "aud": SETTINGS.audience,
               "iat": now, "exp": now + 600}
    payload.update(over)
    payload = {k: v for k, v in payload.items() if v is not None}  # None means "omit claim"
    return jwt.encode(payload, SECRET, algorithm="HS256")


def b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_valid_token(role: str) -> None:
    p = authenticate(f"Bearer {token(role)}", SETTINGS)
    assert (p.subject, p.role) == ("alice", role)


def test_minted_token_round_trips() -> None:
    t = mint_token(SETTINGS, subject="bob", role="admin", ttl_seconds=60, now=int(time.time()))
    assert authenticate(f"Bearer {t}", SETTINGS).role == "admin"


@pytest.mark.parametrize("header", ["bearer {t}", "BEARER {t}", "  Bearer   {t}  "])
def test_scheme_is_case_insensitive_and_whitespace_tolerant(header: str) -> None:
    assert authenticate(header.format(t=token()), SETTINGS).role == "viewer"


@pytest.mark.parametrize("header, reason", [
    (None, "missing_token"),
    ("", "missing_token"),
    ("Bearer", "malformed_authorization_header"),
    ("Basic dXNlcjpwYXNz", "malformed_authorization_header"),
    ("Bearer a b", "malformed_authorization_header"),
    ("Bearer not.a.jwt", "invalid_token"),
])
def test_malformed_headers(header, reason: str) -> None:
    with pytest.raises(AuthError) as exc:
        authenticate(header, SETTINGS)
    assert exc.value.reason == reason


def test_expired_token() -> None:
    old = int(time.time()) - 3600
    with pytest.raises(AuthError) as exc:
        authenticate(f"Bearer {token(iat=old, exp=old + 60)}", SETTINGS)
    assert exc.value.reason == "token_expired"


def test_wrong_signature() -> None:
    forged = jwt.encode({"sub": "x", "role": "admin", "iss": SETTINGS.issuer, "aud": SETTINGS.audience,
                         "iat": int(time.time()), "exp": int(time.time()) + 60}, "x" * 48, algorithm="HS256")
    with pytest.raises(AuthError) as exc:
        authenticate(f"Bearer {forged}", SETTINGS)
    assert exc.value.reason == "invalid_token"


def test_alg_none_rejected() -> None:
    now = int(time.time())
    unsigned = b64({"alg": "none", "typ": "JWT"}) + "." + b64(
        {"sub": "x", "role": "admin", "iss": SETTINGS.issuer, "aud": SETTINGS.audience, "iat": now, "exp": now + 60}) + "."
    with pytest.raises(AuthError):
        authenticate(f"Bearer {unsigned}", SETTINGS)


def test_other_hmac_algorithm_rejected() -> None:
    now = int(time.time())
    t = jwt.encode({"sub": "x", "role": "admin", "iss": SETTINGS.issuer, "aud": SETTINGS.audience,
                    "iat": now, "exp": now + 60}, SECRET, algorithm="HS512")
    with pytest.raises(AuthError):
        authenticate(f"Bearer {t}", SETTINGS)


@pytest.mark.parametrize("over", [
    {"aud": "someone-else"},
    {"iss": "someone-else"},
    {"exp": None},
    {"iat": None},
    {"sub": None},
    {"role": None},
])
def test_claim_validation(over: dict) -> None:
    with pytest.raises(AuthError):
        authenticate(f"Bearer {token(**over)}", SETTINGS)


@pytest.mark.parametrize("role", ["root", "Admin", "", "admin ", ["admin"]])
def test_unknown_roles_fail_closed(role) -> None:
    with pytest.raises(AuthError) as exc:
        authenticate(f"Bearer {token(role=role)}", SETTINGS)
    assert exc.value.reason == "invalid_role"


def test_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        AuthSettings(secret="too-short", issuer="i", audience="a")


def test_extract_bearer() -> None:
    assert extract_bearer("Bearer abc") == "abc"
