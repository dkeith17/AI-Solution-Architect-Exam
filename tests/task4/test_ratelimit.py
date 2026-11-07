"""SQLite sliding-window token limiter (stdlib only)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from task4_router.ratelimit import Denied, Reservation, Tenant, TokenRateLimiter, hash_key


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(tmp_path: Path, clock: Clock | None = None, window: float = 60.0) -> TokenRateLimiter:
    return TokenRateLimiter(tmp_path / "rl.sqlite3", window_seconds=window, clock=clock or Clock())


T = Tenant("acme", 50_000)


def test_admits_up_to_the_limit_then_denies(tmp_path: Path) -> None:
    rl = make(tmp_path)
    assert isinstance(rl.reserve(T, 30_000), Reservation)
    assert isinstance(rl.reserve(T, 20_000), Reservation)   # exactly at the limit
    d = rl.reserve(T, 1)
    assert isinstance(d, Denied) and d.reason == "rate_limited" and d.used == 50_000


def test_request_larger_than_limit_is_never_admitted(tmp_path: Path) -> None:
    d = make(tmp_path).reserve(T, 50_001)
    assert isinstance(d, Denied) and d.reason == "exceeds_limit" and d.retry_after is None


def test_window_slides_rather_than_resetting(tmp_path: Path) -> None:
    clock = Clock()
    rl = make(tmp_path, clock)
    rl.reserve(T, 30_000)            # t=0
    clock.t += 30
    rl.reserve(T, 20_000)            # t=30
    clock.t += 29                    # t=59: both still inside the window
    assert isinstance(rl.reserve(T, 1), Denied)
    clock.t += 1                     # t=60: the t=0 entry reaches the edge and expires
    assert isinstance(rl.reserve(T, 30_000), Reservation)
    assert isinstance(rl.reserve(T, 1), Denied)


def test_retry_after_points_at_when_enough_capacity_frees(tmp_path: Path) -> None:
    clock = Clock()
    rl = make(tmp_path, clock)
    rl.reserve(T, 10_000)            # t=0
    clock.t += 10
    rl.reserve(T, 30_000)            # t=10
    clock.t += 5
    rl.reserve(T, 10_000)            # t=15 -> used 50k
    clock.t += 5                     # t=20
    d = rl.reserve(T, 15_000)        # needs 15k freed: t=0 (10k) is not enough, t=10 (30k) is
    assert isinstance(d, Denied)
    assert d.retry_after == 10 + 60 - 20   # t=10 entry expires at t=70; now is t=20
    clock.t += d.retry_after
    assert isinstance(rl.reserve(T, 15_000), Reservation)


def test_expired_rows_are_evicted(tmp_path: Path) -> None:
    clock = Clock()
    rl = make(tmp_path, clock)
    for _ in range(5):
        rl.reserve(T, 100)
    assert rl.row_count() == 5
    clock.t += 61
    rl.reserve(T, 100)               # reserve evicts this tenant's expired rows
    assert rl.row_count() == 1
    assert rl.used("acme") == 100


def test_sweep_evicts_idle_tenants(tmp_path: Path) -> None:
    clock = Clock()
    rl = make(tmp_path, clock)
    rl.reserve(Tenant("idle", 1000), 10)
    rl.reserve(T, 10)
    clock.t += 61
    assert rl.sweep() == 2
    assert rl.row_count() == 0


def test_reconcile_releases_overestimate(tmp_path: Path) -> None:
    rl = make(tmp_path)
    r = rl.reserve(T, 40_000)
    assert isinstance(rl.reserve(T, 20_000), Denied)
    rl.reconcile(r, 1_500)           # provider reported far fewer tokens
    assert rl.used("acme") == 1_500
    assert isinstance(rl.reserve(T, 20_000), Reservation)


def test_release_removes_reservation(tmp_path: Path) -> None:
    rl = make(tmp_path)
    r = rl.reserve(T, 50_000)
    rl.release(r)
    assert rl.used("acme") == 0


def test_tenants_are_isolated(tmp_path: Path) -> None:
    rl = make(tmp_path)
    rl.reserve(T, 50_000)
    assert isinstance(rl.reserve(Tenant("other", 50_000), 50_000), Reservation)


def test_concurrent_reservations_never_exceed_limit(tmp_path: Path) -> None:
    rl = make(tmp_path)
    results: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(25)

    def worker() -> None:
        barrier.wait()
        r = rl.reserve(T, 5_000)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    admitted = [r for r in results if isinstance(r, Reservation)]
    assert len(admitted) == 10                      # 10 x 5k = 50k
    assert rl.used("acme") == 50_000


def test_concurrent_across_separate_limiter_instances(tmp_path: Path) -> None:
    # Separate instances share only the file, like separate gateway processes.
    path = tmp_path / "shared.sqlite3"
    limiters = [TokenRateLimiter(path) for _ in range(4)]
    results: list[object] = []
    lock = threading.Lock()

    def worker(rl: TokenRateLimiter) -> None:
        for _ in range(5):
            r = rl.reserve(T, 4_000)
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker, args=(rl,)) for rl in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, Reservation) for r in results) == 12   # floor(50k / 4k)


def test_tenant_keys_are_stored_hashed(tmp_path: Path) -> None:
    rl = make(tmp_path)
    rl.add_tenant("qk_super_secret_value", "acme", 50_000)
    assert rl.lookup_tenant("qk_super_secret_value") == Tenant("acme", 50_000)
    assert rl.lookup_tenant("wrong") is None
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("rl.sqlite3*"))   # db + WAL files
    assert b"qk_super_secret_value" not in raw
    conn = sqlite3.connect(tmp_path / "rl.sqlite3")
    try:
        stored = conn.execute("SELECT key_hash FROM tenants").fetchall()
    finally:
        conn.close()
    assert stored == [(hash_key("qk_super_secret_value"),)]


def test_invalid_window_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TokenRateLimiter(tmp_path / "x.sqlite3", window_seconds=0)
