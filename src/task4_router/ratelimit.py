"""Token-aware sliding-window rate limiter backed by an on-disk SQLite database.

Model: a sliding *log*. Every admitted request inserts a row (tenant, timestamp,
tokens). A tenant's usage is the sum of its rows newer than ``now - window``.
Unlike fixed windows there is no boundary burst: at any instant, the trailing
60 seconds never exceed the limit (modulo reconciliation, below).

Reserve / reconcile
-------------------
The true token count is known only after the provider answers, so admission
*reserves* an upper-bound estimate (prompt estimate + max_tokens) and the row is
later *reconciled* to the provider-reported usage. Usually that releases capacity.

Atomicity
---------
Each operation runs in its own ``BEGIN IMMEDIATE`` transaction, which takes
SQLite's write lock up front. Check-then-insert is therefore atomic across
threads *and* processes sharing the file; concurrent reservations cannot both see
the same free capacity. WAL mode lets readers proceed during writes.

Eviction
--------
Rows at or beyond the window edge are deleted inside every reserve for that tenant
(so a tenant's state is exact whenever it is checked), and ``sweep`` removes stale
rows for all tenants (the app runs it periodically so idle tenants do not leave
rows behind).

API keys are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_TOKENS_PER_MINUTE = 50_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    key_hash          TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL UNIQUE,
    tokens_per_minute INTEGER NOT NULL CHECK (tokens_per_minute > 0)
);
CREATE TABLE IF NOT EXISTS token_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    tokens    INTEGER NOT NULL CHECK (tokens >= 0)
);
CREATE INDEX IF NOT EXISTS ix_token_usage_tenant_ts ON token_usage (tenant_id, ts);
"""


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    tokens_per_minute: int


@dataclass(frozen=True)
class Reservation:
    id: int
    tenant_id: str
    tokens: int


@dataclass(frozen=True)
class Denied:
    reason: str               # "rate_limited" | "exceeds_limit"
    limit: int
    used: int
    retry_after: float | None  # seconds until enough capacity frees up; None if never


class TokenRateLimiter:
    def __init__(self, db_path: str | Path, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.time, busy_timeout_s: float = 10.0) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.db_path = str(db_path)
        self.window = float(window_seconds)
        self.clock = clock
        self.busy_timeout_s = busy_timeout_s
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    # -- connection handling ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit; transactions are opened explicitly.
        return sqlite3.connect(self.db_path, timeout=self.busy_timeout_s, isolation_level=None)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    # -- tenants -----------------------------------------------------------------------------

    def add_tenant(self, api_key: str, tenant_id: str, tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE) -> Tenant:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tenants (key_hash, tenant_id, tokens_per_minute) VALUES (?, ?, ?)",
                (hash_key(api_key), tenant_id, tokens_per_minute),
            )
        return Tenant(tenant_id, tokens_per_minute)

    def lookup_tenant(self, api_key: str) -> Tenant | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT tenant_id, tokens_per_minute FROM tenants WHERE key_hash = ?", (hash_key(api_key),)
            ).fetchone()
        finally:
            conn.close()
        return Tenant(row[0], row[1]) if row else None

    def list_tenants(self) -> list[Tenant]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT tenant_id, tokens_per_minute FROM tenants ORDER BY tenant_id").fetchall()
        finally:
            conn.close()
        return [Tenant(r[0], r[1]) for r in rows]

    # -- limiter -------------------------------------------------------------------------

    def reserve(self, tenant: Tenant, tokens: int) -> Reservation | Denied:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        limit = tenant.tokens_per_minute
        now = self.clock()
        edge = now - self.window
        with self._tx() as conn:
            conn.execute("DELETE FROM token_usage WHERE tenant_id = ? AND ts <= ?", (tenant.tenant_id, edge))
            rows = conn.execute(
                "SELECT ts, tokens FROM token_usage WHERE tenant_id = ? ORDER BY ts, id", (tenant.tenant_id,)
            ).fetchall()
            used = sum(r[1] for r in rows)
            if tokens > limit:
                return Denied("exceeds_limit", limit, used, None)
            if used + tokens > limit:
                need = used + tokens - limit
                freed = 0
                retry_after = self.window
                for ts, t in rows:
                    freed += t
                    if freed >= need:
                        retry_after = max(0.0, ts + self.window - now)
                        break
                return Denied("rate_limited", limit, used, retry_after)
            cur = conn.execute(
                "INSERT INTO token_usage (tenant_id, ts, tokens) VALUES (?, ?, ?)", (tenant.tenant_id, now, tokens)
            )
            return Reservation(int(cur.lastrowid), tenant.tenant_id, tokens)

    def reconcile(self, reservation: Reservation, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        with self._tx() as conn:
            conn.execute("UPDATE token_usage SET tokens = ? WHERE id = ?", (actual_tokens, reservation.id))

    def release(self, reservation: Reservation) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM token_usage WHERE id = ?", (reservation.id,))

    def used(self, tenant_id: str) -> int:
        edge = self.clock() - self.window
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_id = ? AND ts > ?", (tenant_id, edge)
            ).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def sweep(self) -> int:
        """Delete expired rows for every tenant. Returns the number removed."""
        edge = self.clock() - self.window
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM token_usage WHERE ts <= ?", (edge,))
            return cur.rowcount

    def row_count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0])
        finally:
            conn.close()
