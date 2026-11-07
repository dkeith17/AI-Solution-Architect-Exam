"""Raw stdio wire tests: spawn the real server and speak newline-delimited JSON-RPC.

These tests do not use the SDK client, so they observe exactly what the process
writes. Every stdout line must be a JSON-RPC 2.0 message; logs must be on stderr.
The fault-injection run makes the tool handler call print(), write to fd 1
directly, and spawn a child that prints, and stdout must still be pure.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

from tests.conftest import ROOT, subprocess_env

pytestmark = pytest.mark.integration

HANDSHAKE_VERSION = "2025-11-25"


class StdioSession:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "task1_mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=subprocess_env(env),
            cwd=str(ROOT),
        )
        self.stdout_lines: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self._q: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._next_id = 1

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.stdout_lines.append(line)
            self._q.put(line)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
            self.stderr_chunks.append(chunk)

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        rid = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            obj = json.loads(line)
            if obj.get("id") == rid:
                return obj
        raise TimeoutError(f"no response to {method}")

    def initialize(self) -> dict:
        resp = self.request("initialize", {
            "protocolVersion": HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "raw-stdio-test", "version": "1.0"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def close(self) -> str:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        time.sleep(0.2)
        return b"".join(self.stderr_chunks).decode("utf-8", errors="replace")


def assert_pure_jsonrpc(lines: list[bytes]) -> None:
    assert lines, "server wrote nothing to stdout"
    for raw in lines:
        text = raw.decode("utf-8")
        assert text.endswith("\n"), "every message must be newline-terminated"
        obj = json.loads(text)  # raises if any non-JSON bytes reached stdout
        assert isinstance(obj, dict) and obj.get("jsonrpc") == "2.0", text
        assert ("result" in obj) ^ ("error" in obj) or "method" in obj, text


@pytest.fixture
def session():
    sessions: list[StdioSession] = []

    def make(env: dict[str, str] | None = None) -> StdioSession:
        s = StdioSession(env)
        sessions.append(s)
        return s

    yield make
    for s in sessions:
        if s.proc.poll() is None:
            s.proc.kill()


def test_handshake_and_tools_list_on_pure_stdout(session) -> None:
    s = session()
    init = s.initialize()
    assert "result" in init and init["result"]["serverInfo"]["name"] == "quilr-task1-customer-service"
    tools = s.request("tools/list", {})
    assert {t["name"] for t in tools["result"]["tools"]} == {"get_customer_record", "trigger_refund"}
    stderr = s.close()
    assert_pure_jsonrpc(s.stdout_lines)
    assert "starting stdio server" in stderr  # logs land on stderr


def test_invalid_input_is_jsonrpc_32602_on_the_wire(session) -> None:
    s = session()
    s.initialize()
    resp = s.request("tools/call", {"name": "get_customer_record", "arguments": {"customer_id": "bad"}})
    assert resp["error"]["code"] == -32602
    assert resp["error"]["data"]["errors"][0]["type"] == "customer_id_format"
    resp = s.request("tools/call", {"name": "trigger_refund",
                                    "arguments": {"customer_id": "CUST-00001", "amount": 0.001, "reason": "x"}})
    assert resp["error"]["code"] == -32602
    types = {e["type"] for e in resp["error"]["data"]["errors"]}
    assert types == {"amount_precision", "reason_too_short"}
    resp = s.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    assert resp["error"]["code"] == -32602
    s.close()
    assert_pure_jsonrpc(s.stdout_lines)


def test_valid_call_and_business_error_on_the_wire(session) -> None:
    s = session()
    s.initialize()
    ok = s.request("tools/call", {"name": "trigger_refund", "arguments": {
        "customer_id": "CUST-ZZ999", "amount": 10, "reason": "Duplicate charge on invoice"}})
    assert ok["result"].get("isError") in (None, False)
    assert ok["result"]["structuredContent"]["amount"] == "10.00"
    missing = s.request("tools/call", {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00000"}})
    assert missing["result"]["isError"] is True
    s.close()
    assert_pure_jsonrpc(s.stdout_lines)


def test_sanitizer_logs_go_to_stderr_not_stdout(session) -> None:
    s = session()
    s.initialize()
    s.request("tools/call", {"name": "trigger_refund", "arguments": {
        "customer_id": "CUST-00001", "amount": 5, "reason": "refund\u202e please, thanks"}})
    stderr = s.close()
    assert_pure_jsonrpc(s.stdout_lines)
    assert "remove_format_chars" in stderr
    assert all(b"remove_format_chars" not in line for line in s.stdout_lines)


def test_stdout_stays_pure_under_fault_injection(session) -> None:
    s = session({"QUILR_T1_FAULT_INJECT_STDOUT": "1"})
    s.initialize()
    for _ in range(3):
        resp = s.request("tools/call", {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00001"}})
        assert "result" in resp
    stderr = s.close()
    assert_pure_jsonrpc(s.stdout_lines)
    joined = b"".join(s.stdout_lines)
    assert b"FAULT-INJECTION" not in joined
    assert "FAULT-INJECTION: print() to stdout" in stderr
    assert "FAULT-INJECTION: raw write to fd 1" in stderr
    assert "FAULT-INJECTION: child process stdout" in stderr
