"""Shared pytest fixtures.

Async tests use the anyio pytest plugin (shipped with anyio, which the MCP SDK
depends on), as the MCP Python SDK testing docs prescribe: mark tests with
``@pytest.mark.anyio`` and provide an ``anyio_backend`` fixture.

Integration tests (``@pytest.mark.integration``) start real servers as
subprocesses on free localhost ports and always terminate them.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_log(log) -> str:
    log.seek(0)
    return log.read().decode("utf-8", errors="replace")


def wait_for_port(port: int, proc: subprocess.Popen, log, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (code {proc.returncode}):\n{_read_log(log)}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} did not open within {timeout}s")


def subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


class ServerFactory:
    """Starts ``python -m <module> --port <free port>`` processes and cleans them up."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []
        self.logs: list = []

    def start(self, target: list[str], env: dict[str, str] | None = None, port: int | None = None) -> int:
        port = port or free_port()
        # Server logs go to a temp file, not a pipe: an undrained pipe fills up
        # (quickly on Windows) and blocks the server mid-request.
        log = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            [sys.executable, *target, "--port", str(port)],
            env=subprocess_env(env),
            stdout=subprocess.DEVNULL,
            stderr=log,
            cwd=str(ROOT),
        )
        self.procs.append(proc)
        self.logs.append(log)
        wait_for_port(port, proc, log)
        return port

    def log_text(self, index: int = -1) -> str:
        return _read_log(self.logs[index])

    def stop_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()  # TerminateProcess on Windows, SIGTERM elsewhere
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for log in self.logs:
            log.close()


@pytest.fixture
def servers() -> Iterator[ServerFactory]:
    factory = ServerFactory()
    try:
        yield factory
    finally:
        factory.stop_all()


@pytest.fixture
def make_servers() -> Iterator[Callable[[], ServerFactory]]:
    factories: list[ServerFactory] = []

    def make() -> ServerFactory:
        f = ServerFactory()
        factories.append(f)
        return f

    try:
        yield make
    finally:
        for f in factories:
            f.stop_all()
