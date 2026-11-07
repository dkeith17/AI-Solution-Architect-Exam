"""Token utility.

    python -m task2_gateway.mint_token --gen-secret
    python -m task2_gateway.mint_token --role admin --sub alice [--ttl 3600]

Signing settings come from the same environment variables the gateway reads
(GATEWAY_JWT_SECRET, GATEWAY_JWT_ISSUER, GATEWAY_JWT_AUDIENCE). This is a CLI, so
it prints its result to stdout.
"""

from __future__ import annotations

import argparse
import secrets
import time

from .app import GatewaySettings
from .auth import KNOWN_ROLES, mint_token


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mint gateway test tokens")
    parser.add_argument("--gen-secret", action="store_true", help="print a new random 256-bit secret")
    parser.add_argument("--role", choices=sorted(KNOWN_ROLES))
    parser.add_argument("--sub", default="demo-user")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)

    if args.gen_secret:
        print(secrets.token_urlsafe(48))
        return
    if not args.role:
        parser.error("--role is required unless --gen-secret is given")
    settings = GatewaySettings.from_env()
    print(mint_token(settings.auth, subject=args.sub, role=args.role, ttl_seconds=args.ttl, now=int(time.time())))


if __name__ == "__main__":
    main()
