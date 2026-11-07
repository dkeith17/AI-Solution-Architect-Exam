"""Primary/secondary model router with timeout-based and 429-based failover.

Failover triggers (the exam's two, plus two opt-in flags):
  * primary answers HTTP 429                              -> try secondary
  * primary does not finish within ``primary_timeout_s``  -> cancel it, try secondary
  * (opt-in) primary answers 5xx                          -> try secondary
  * (opt-in) primary connection/transport failure         -> try secondary
Anything else (for example 400 or 401 from the primary) is not retried elsewhere:
sending a request the primary rejected as malformed to another provider would only
hide the bug.

Timeout race: the call runs inside ``asyncio.timeout``. When it fires, the primary
call is cancelled and awaited before the secondary starts, so at most one upstream
request per gateway request is ever in flight and a late primary answer cannot be
delivered or billed alongside the secondary's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from llm_providers.adapters import (
    ChatRequest,
    Completion,
    ProviderError,
    ProviderHTTPError,
    ProviderTransportError,
)

log = logging.getLogger("task4.router")


class CompletionProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, req: ChatRequest) -> Completion: ...


@dataclass(frozen=True)
class Attempt:
    provider: str
    outcome: str  # ok | rate_limited | timeout | http_error | transport_error | protocol_error
    elapsed_ms: float
    status: int | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class RouteResult:
    completion: Completion
    attempts: tuple[Attempt, ...]

    @property
    def failed_over(self) -> bool:
        return len(self.attempts) > 1


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]) -> None:
        self.attempts = tuple(attempts)
        super().__init__("; ".join(f"{a.provider}:{a.outcome}" for a in attempts))

    @property
    def upstream_did_no_work(self) -> bool:
        """True when every attempt was refused with 429 (no tokens were consumed)."""
        return all(a.outcome == "rate_limited" for a in self.attempts)


@dataclass
class ModelRouter:
    primary: CompletionProvider
    secondary: CompletionProvider
    primary_timeout_s: float = 3.0
    secondary_timeout_s: float = 3.0
    failover_on_5xx: bool = False
    failover_on_transport_error: bool = False
    clock: Callable[[], float] = field(default=time.perf_counter)

    async def _attempt(self, provider: CompletionProvider, req: ChatRequest,
                       timeout_s: float) -> tuple[Completion | None, Attempt]:
        start = self.clock()

        def ms() -> float:
            return (self.clock() - start) * 1000

        try:
            async with asyncio.timeout(timeout_s):
                completion = await provider.complete(req)
            return completion, Attempt(provider.name, "ok", ms())
        except TimeoutError:
            return None, Attempt(provider.name, "timeout", ms())
        except ProviderHTTPError as exc:
            outcome = "rate_limited" if exc.status == 429 else "http_error"
            return None, Attempt(provider.name, outcome, ms(), exc.status, exc.retry_after)
        except ProviderTransportError:
            return None, Attempt(provider.name, "transport_error", ms())
        except ProviderError:
            return None, Attempt(provider.name, "protocol_error", ms())

    def _should_failover(self, a: Attempt) -> bool:
        if a.outcome in ("rate_limited", "timeout"):
            return True
        if a.outcome == "http_error" and a.status is not None and a.status >= 500:
            return self.failover_on_5xx
        if a.outcome == "transport_error":
            return self.failover_on_transport_error
        return False

    async def complete(self, req: ChatRequest) -> RouteResult:
        attempts: list[Attempt] = []
        completion, attempt = await self._attempt(self.primary, req, self.primary_timeout_s)
        attempts.append(attempt)
        if completion is not None:
            return RouteResult(completion, tuple(attempts))
        log.warning("primary %s failed: %s (%.0f ms)", attempt.provider, attempt.outcome, attempt.elapsed_ms)
        if not self._should_failover(attempt):
            raise AllProvidersFailed(attempts)

        completion, attempt = await self._attempt(self.secondary, req, self.secondary_timeout_s)
        attempts.append(attempt)
        if completion is not None:
            log.info("failover to %s succeeded", attempt.provider)
            return RouteResult(completion, tuple(attempts))
        log.error("secondary %s failed: %s", attempt.provider, attempt.outcome)
        raise AllProvidersFailed(attempts)
