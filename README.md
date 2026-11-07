# QuilrAI Solution Architect Practical Exam: Python Implementation

Four runnable components answering the four exam tasks, with a pytest suite and
Windows 11 instructions. All four tasks are implemented in Python; Tasks 1 and 2
were required in Python, and Tasks 3 and 4 were implemented as well, each with a
design document.

| Task | Package | What it is |
|------|---------|------------|
| 1 | `src/task1_mcp_server` | MCP server over stdio (official SDK v2), two tools, Pydantic validation, input sanitization and injection policy, JSON-RPC `-32602` for invalid input |
| 2 | `src/task2_gateway` | MCP security gateway: reverse proxy for Streamable HTTP, HS256 JWT role auth, `admin_*` tool-call authorization with `-32001` |
| 3 | `src/task3_llm_stream` | LLM gateway streaming endpoint with real-time, bounded-memory PII redaction |
| 4 | `src/task4_router` | LLM gateway completions with a SQLite sliding-window token limiter and 429/3000 ms failover |
| shared | `src/llm_providers` | OpenAI-compatible and Anthropic wire adapters, SSE parser, mock upstream |

## Quick start (Windows 11, PowerShell)

```powershell
winget install 9NQ7512CXL7T          # Python install manager (or install it from the Microsoft Store)
py install 3.14
cd quilr-sa-exam
py -V:3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

Full instructions, including how to run each server by hand: [docs/INSTALL_AND_RUN_WINDOWS.md](docs/INSTALL_AND_RUN_WINDOWS.md).

## Documentation

* [Install and run on Windows 11](docs/INSTALL_AND_RUN_WINDOWS.md)
* [Task 1: MCP server](docs/TASK1_MCP_SERVER.md)
* [Task 2: MCP security gateway](docs/TASK2_MCP_GATEWAY.md)
* [Task 3: Streaming PII guardrail](docs/TASK3_STREAMING_PII_GUARDRAIL.md)
* [Task 4: Rate limiting and failover](docs/TASK4_RATE_LIMIT_FAILOVER.md)
* [Testing](docs/TESTING.md)
* [Component verification](docs/VERIFICATION.md): every external component, its version, and the source used to confirm it
* [Decision log](docs/DECISIONS.md): every choice that was asked about rather than assumed

## Two conflicts in the exam text, resolved as instructed

1. **Task 1 error codes vs. the current MCP spec.** The exam requires invalid input
   to be rejected with JSON-RPC error codes. The current spec (SEP-1303, Final)
   says tool input-validation failures should be tool results with `isError: true`
   so the model can self-correct. This implementation follows the exam (`-32602`),
   by decision. See the decision log.
2. **Task 2's `-32001`.** The exam assigns `-32001` to "Unauthorized Tool Call". In
   MCP Python SDK v2 (and the TypeScript SDK), `-32001` is the client-side
   `REQUEST_TIMEOUT` code, so a generic client may label a denied call as a timeout.
   The gateway uses `-32001` as required and adds `error.data.required_role` so
   clients can tell them apart.

## Repository layout

```
pyproject.toml            dependencies (mcp==2.3.0) and pytest config
config/                   Task 1 sanitizer policy (TOML)
scripts/                  setup.ps1, run-tests.ps1
src/                      the five packages above
tests/                    task1..task4 unit and integration tests, shared fixtures
docs/                     design, install, testing, verification, decisions
```
