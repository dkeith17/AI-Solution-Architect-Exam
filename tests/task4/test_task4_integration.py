"""End-to-end: tenant auth, SQLite rate limiting, failover, standardized errors."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx2
import pytest

from task4_router.ratelimit import TokenRateLimiter

pytestmark = pytest.mark.integration

ERROR_KEYS = {"type", "message", "request_id"}
LEAK_MARKERS = ("Traceback", "mock status", "mock_error", "127.0.0.1", "httpx", "Exception")


@pytest.fixture
def upstream(servers) -> int:
    return servers.start(["-m", "llm_providers.mock_upstream"])


@pytest.fixture
def gateway(servers, upstream, tmp_path: Path):
    def start(primary_model: str = "mock-echo", secondary_model: str = "mock-echo",
              tpm: int = 50_000, **env) -> tuple[str, str]:
        db = tmp_path / f"router-{len(servers.procs)}.sqlite3"
        key = f"qk_test_{len(servers.procs)}"
        TokenRateLimiter(db).add_tenant(key, "acme", tpm)
        port = servers.start(["-m", "task4_router"], env={
            "T4_DB_PATH": str(db),
            "MOCK_UPSTREAM_URL": f"http://127.0.0.1:{upstream}",
            "MOCK_OPENAI_MODEL": primary_model,
            "MOCK_ANTHROPIC_MODEL": secondary_model,
            "T4_PRIMARY_PROVIDER": "mock-openai",
            "T4_SECONDARY_PROVIDER": "mock-anthropic",
            **env,
        })
        return f"http://127.0.0.1:{port}", key
    return start


def post(base: str, key: str | None, body: dict | bytes, timeout: float = 30) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if isinstance(body, bytes):
        return httpx2.post(base + "/v1/completions", content=body, headers=headers, timeout=timeout)
    return httpx2.post(base + "/v1/completions", json=body, headers=headers, timeout=timeout)


BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}


def assert_standard_error(r: httpx2.Response, status: int, etype: str) -> None:
    assert r.status_code == status
    payload = r.json()
    assert set(payload) == {"error"} and set(payload["error"]) == ERROR_KEYS
    assert payload["error"]["type"] == etype
    assert payload["error"]["request_id"] == r.headers.get("x-request-id", payload["error"]["request_id"])
    text = json.dumps(payload)
    for marker in LEAK_MARKERS:
        assert marker not in text, marker


def test_primary_success(gateway) -> None:
    base, key = gateway()
    r = post(base, key, BODY)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "mock-openai" and data["failover"] is False
    assert data["content"] == "Echo: hi"
    assert r.headers["x-gateway-provider"] == "mock-openai"
    assert int(r.headers["x-ratelimit-remaining-tokens"]) < 50_000


def test_primary_429_fails_over_to_secondary(gateway) -> None:
    base, key = gateway(primary_model="mock-429")
    r = post(base, key, BODY)
    assert r.status_code == 200
    assert r.json()["provider"] == "mock-anthropic" and r.json()["failover"] is True


def test_primary_timeout_fails_over_after_3000ms(gateway) -> None:
    base, key = gateway(primary_model="mock-slow-5000")
    start = time.perf_counter()
    r = post(base, key, BODY)
    elapsed = time.perf_counter() - start
    assert r.status_code == 200 and r.json()["provider"] == "mock-anthropic"
    assert 3.0 <= elapsed < 4.5


def test_primary_under_deadline_is_not_failed_over(gateway) -> None:
    base, key = gateway(primary_model="mock-slow-1500")
    r = post(base, key, BODY)
    assert r.json()["provider"] == "mock-openai"


def test_both_rate_limited_is_503_standardized(gateway) -> None:
    base, key = gateway(primary_model="mock-429", secondary_model="mock-429")
    r = post(base, key, BODY)
    assert_standard_error(r, 503, "upstream_rate_limited")


def test_both_timing_out_is_504_standardized(gateway) -> None:
    base, key = gateway(primary_model="mock-slow-5000", secondary_model="mock-slow-5000")
    r = post(base, key, BODY)
    assert_standard_error(r, 504, "upstream_timeout")


def test_primary_500_is_502_without_failover_by_default(gateway) -> None:
    base, key = gateway(primary_model="mock-500")
    assert_standard_error(post(base, key, BODY), 502, "upstream_error")


def test_primary_500_fails_over_when_enabled(gateway) -> None:
    base, key = gateway(primary_model="mock-500", T4_FAILOVER_ON_5XX="true")
    assert post(base, key, BODY).json()["provider"] == "mock-anthropic"


@pytest.mark.parametrize("key", [None, "qk_wrong"])
def test_authentication(gateway, key) -> None:
    base, _ = gateway()
    assert_standard_error(post(base, key, BODY), 401, "authentication_error")


@pytest.mark.parametrize("body", [
    b"{not json",
    {"messages": [], "max_tokens": 5},
    {"messages": [{"role": "user", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x"}], "max_tokens": True},
    {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5, "model": "pick-me"},
])
def test_invalid_bodies(gateway, body) -> None:
    base, key = gateway()
    assert_standard_error(post(base, key, body), 400, "invalid_request")


def test_unknown_route_is_standardized(gateway) -> None:
    base, _ = gateway()
    r = httpx2.get(base + "/v1/nope")
    assert_standard_error(r, 404, "not_found")


def test_request_larger_than_tenant_limit(gateway) -> None:
    base, key = gateway(tpm=1000)
    r = post(base, key, {"messages": [{"role": "user", "content": "x"}], "max_tokens": 2000})
    assert_standard_error(r, 400, "request_too_large")


def test_rate_limit_429_with_retry_after_while_reservation_is_held(gateway) -> None:
    # tpm=1000; each request reserves ~607 tokens. The first is slow (1.5 s), so its
    # reservation is still held when the second arrives.
    base, key = gateway(primary_model="mock-slow-1500", tpm=1000)
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 600}
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("r", post(base, key, body)))
    t.start()
    time.sleep(0.5)
    second = post(base, key, body)
    t.join()
    assert first["r"].status_code == 200
    assert_standard_error(second, 429, "rate_limit_exceeded")
    assert int(second.headers["retry-after"]) >= 1


def test_reconciled_usage_frees_capacity(gateway) -> None:
    # Reservations are ~607 tokens but actual usage is tiny, so sequential requests
    # keep succeeding well past 1000 / 607.
    base, key = gateway(tpm=1000)
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 600}
    for _ in range(5):
        assert post(base, key, body).status_code == 200


def test_state_is_on_disk_sqlite(gateway, tmp_path: Path) -> None:
    base, key = gateway()
    post(base, key, BODY)
    dbs = list(tmp_path.glob("router-*.sqlite3"))
    assert dbs and dbs[0].stat().st_size > 0
