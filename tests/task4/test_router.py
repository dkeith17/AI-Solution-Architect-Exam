"""Failover router with fake providers (no network)."""

from __future__ import annotations

import asyncio
import time

import pytest

from llm_providers.adapters import (
    ChatRequest,
    Completion,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTransportError,
    Usage,
)
from task4_router.router import AllProvidersFailed, ModelRouter

pytestmark = pytest.mark.anyio

REQ = ChatRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)


class Fake:
    def __init__(self, name: str, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self._name = name
        self.delay = delay
        self.error = error
        self.calls = 0
        self.completed = 0
        self.cancelled = 0

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, req: ChatRequest) -> Completion:
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error:
            raise self.error
        self.completed += 1
        return Completion(text=f"from {self._name}", usage=Usage(3, 4), model="m", provider=self._name)


def router(primary: Fake, secondary: Fake, **kw) -> ModelRouter:
    kw.setdefault("primary_timeout_s", 0.3)
    kw.setdefault("secondary_timeout_s", 0.3)
    return ModelRouter(primary=primary, secondary=secondary, **kw)


async def test_primary_success_does_not_touch_secondary() -> None:
    p, s = Fake("p"), Fake("s")
    res = await router(p, s).complete(REQ)
    assert res.completion.provider == "p" and not res.failed_over and s.calls == 0


async def test_429_fails_over() -> None:
    p, s = Fake("p", error=ProviderHTTPError(429, 2.0)), Fake("s")
    res = await router(p, s).complete(REQ)
    assert res.completion.provider == "s" and res.failed_over
    assert [a.outcome for a in res.attempts] == ["rate_limited", "ok"]
    assert res.attempts[0].retry_after == 2.0


async def test_timeout_cancels_primary_then_fails_over() -> None:
    p, s = Fake("p", delay=5.0), Fake("s")
    start = time.perf_counter()
    res = await router(p, s, primary_timeout_s=0.2).complete(REQ)
    elapsed = time.perf_counter() - start
    assert res.completion.provider == "s"
    assert res.attempts[0].outcome == "timeout"
    assert p.cancelled == 1 and p.completed == 0     # primary was really cancelled
    assert 0.2 <= elapsed < 1.0


async def test_exam_timeout_is_3000ms_by_default() -> None:
    r = ModelRouter(primary=Fake("p"), secondary=Fake("s"))
    assert r.primary_timeout_s == 3.0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_do_not_fail_over(status: int) -> None:
    p, s = Fake("p", error=ProviderHTTPError(status)), Fake("s")
    with pytest.raises(AllProvidersFailed):
        await router(p, s).complete(REQ)
    assert s.calls == 0


async def test_5xx_fails_over_only_when_enabled() -> None:
    p, s = Fake("p", error=ProviderHTTPError(503)), Fake("s")
    with pytest.raises(AllProvidersFailed):
        await router(p, s).complete(REQ)
    res = await router(p, s, failover_on_5xx=True).complete(REQ)
    assert res.completion.provider == "s"


async def test_transport_error_fails_over_only_when_enabled() -> None:
    p, s = Fake("p", error=ProviderTransportError("ConnectError")), Fake("s")
    with pytest.raises(AllProvidersFailed):
        await router(p, s).complete(REQ)
    assert (await router(p, s, failover_on_transport_error=True).complete(REQ)).completion.provider == "s"


async def test_protocol_error_does_not_fail_over() -> None:
    p, s = Fake("p", error=ProviderProtocolError("bad")), Fake("s")
    with pytest.raises(AllProvidersFailed) as exc:
        await router(p, s).complete(REQ)
    assert exc.value.attempts[0].outcome == "protocol_error"


async def test_both_fail() -> None:
    p, s = Fake("p", error=ProviderHTTPError(429)), Fake("s", delay=5.0)
    with pytest.raises(AllProvidersFailed) as exc:
        await router(p, s, secondary_timeout_s=0.1).complete(REQ)
    assert [a.outcome for a in exc.value.attempts] == ["rate_limited", "timeout"]
    assert not exc.value.upstream_did_no_work


async def test_both_429_means_no_upstream_work() -> None:
    p, s = Fake("p", error=ProviderHTTPError(429)), Fake("s", error=ProviderHTTPError(429))
    with pytest.raises(AllProvidersFailed) as exc:
        await router(p, s).complete(REQ)
    assert exc.value.upstream_did_no_work


async def test_timeout_race_never_double_delivers() -> None:
    # Primary latency straddles the deadline; each request must be answered by
    # exactly one provider, and a timed-out primary must never also complete.
    for i in range(40):
        delay = 0.05 + (i % 5 - 2) * 0.004          # 0.042 .. 0.058 around a 0.05 deadline
        p, s = Fake("p", delay=delay), Fake("s")
        res = await router(p, s, primary_timeout_s=0.05).complete(REQ)
        if res.completion.provider == "p":
            assert p.completed == 1 and s.calls == 0 and p.cancelled == 0
        else:
            assert p.completed == 0 and p.cancelled == 1 and s.calls == 1


async def test_concurrent_requests_are_independent() -> None:
    p, s = Fake("p", delay=0.05), Fake("s")
    results = await asyncio.gather(*(router(p, s).complete(REQ) for _ in range(50)))
    assert all(r.completion.provider == "p" for r in results) and p.completed == 50
