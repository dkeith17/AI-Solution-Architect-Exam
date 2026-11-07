"""Tenant administration CLI (prints to stdout).

    python -m task4_router.admin add-tenant --tenant acme [--tpm 50000]
    python -m task4_router.admin list-tenants
    python -m task4_router.admin usage --tenant acme

Uses T4_DB_PATH (default task4_router.sqlite3). New API keys are shown once; only
their SHA-256 hash is stored.
"""

from __future__ import annotations

import argparse
import os
import secrets

from .ratelimit import DEFAULT_TOKENS_PER_MINUTE, TokenRateLimiter


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Task 4 tenant admin")
    parser.add_argument("--db", default=os.environ.get("T4_DB_PATH", "task4_router.sqlite3"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-tenant")
    add.add_argument("--tenant", required=True)
    add.add_argument("--tpm", type=int, default=DEFAULT_TOKENS_PER_MINUTE)
    sub.add_parser("list-tenants")
    usage = sub.add_parser("usage")
    usage.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)

    limiter = TokenRateLimiter(args.db)
    if args.cmd == "add-tenant":
        key = "qk_" + secrets.token_urlsafe(32)
        limiter.add_tenant(key, args.tenant, args.tpm)
        print(f"tenant={args.tenant} tokens_per_minute={args.tpm}")
        print(f"api_key={key}")
    elif args.cmd == "list-tenants":
        for t in limiter.list_tenants():
            print(f"{t.tenant_id}\t{t.tokens_per_minute}")
    elif args.cmd == "usage":
        print(f"{args.tenant}\t{limiter.used(args.tenant)} tokens in the current window")


if __name__ == "__main__":
    main()
