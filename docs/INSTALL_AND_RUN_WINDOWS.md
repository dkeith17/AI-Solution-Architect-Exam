# Install and run on Windows 11

All commands are for **PowerShell** (Windows Terminal or the PowerShell app), run
from the repository root (the folder containing `pyproject.toml`).

## 1. Install Python 3.14

The CPython team's recommended route on Windows is the **Python install manager**.
Install it from the Microsoft Store, or with WinGet:

```powershell
winget install 9NQ7512CXL7T
```

Then install the 3.14 runtime and confirm it:

```powershell
py install 3.14
py -V:3.14 --version
```

If `py` still points at an older launcher, uninstall the old "Python Launcher"
from Settings > Apps, or run `pymanager install 3.14` instead (the install manager
also provides the unambiguous `pymanager` command).

Why 3.14: the MCP Python SDK v2 requires Python 3.11 or newer, and every compiled
dependency has Windows wheels for 3.14 (see [VERIFICATION.md](VERIFICATION.md)).

## 2. Create a virtual environment and install

```powershell
py -V:3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

This installs `mcp==2.3.0` and its dependencies (Starlette, uvicorn, httpx2,
Pydantic, PyJWT, anyio, pywin32) plus pytest. Or run `scripts\setup.ps1`, which
does the same three steps.

### Optional: activate the environment

Activation lets you type `python` instead of `.\.venv\Scripts\python.exe`:

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses with "running scripts is disabled on this system", allow
locally created scripts for your user once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

The rest of this guide assumes the environment is activated. Without activation,
replace `python` with `.\.venv\Scripts\python.exe`.

The same execution-policy note applies to the `scripts\*.ps1` helpers; you can
also run them as `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.

## 3. Run the tests

```powershell
python -m pytest                       # everything (about 1-2 minutes)
python -m pytest -m "not integration"  # unit tests only (seconds, no servers)
python -m pytest -m integration        # end-to-end tests that start real servers
python -m pytest tests\task2 -v        # one task, verbose
```

Integration tests start servers as subprocesses on free `127.0.0.1` ports and
stop them afterwards. See [TESTING.md](TESTING.md) for what each file covers.

## 4. Run each component by hand

Every server listens on `127.0.0.1` only. Each long-running server needs its own
PowerShell window (activate the environment in each one). Stop a server with
Ctrl+C.

### Task 1: MCP server (stdio)

An stdio MCP server is normally launched by an MCP host, not run interactively.
The demo client launches it as a subprocess and calls both tools, including
invalid inputs:

```powershell
python -m task1_mcp_server.demo_client
```

To launch it from an MCP host, configure the host to run:

```
command: <repo>\.venv\Scripts\python.exe
args:    -m task1_mcp_server
```

Optional environment variables:

| Variable | Effect |
|----------|--------|
| `QUILR_T1_CONFIG` | Path to a sanitizer TOML file, e.g. `config\task1_sanitizer.toml` |
| `QUILR_T1_DEFAULT_ACTION` | `reject` or `allow`; overrides the file's `default_action` |
| `QUILR_T1_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` |
| `QUILR_T1_FAULT_INJECT_STDOUT` | `1` makes tool calls deliberately write to stdout (test hook only) |

Logs always go to stderr; stdout carries only JSON-RPC.

### Task 2: MCP security gateway

Window 1, downstream mock MCP server (port 8001):

```powershell
python -m task2_gateway.downstream_mock --port 8001
```

Window 2, generate a signing secret once, then start the gateway (port 8000):

```powershell
$env:GATEWAY_JWT_SECRET = (python -m task2_gateway.mint_token --gen-secret)
$env:GATEWAY_DOWNSTREAM_URL = "http://127.0.0.1:8001/mcp"
python -m task2_gateway --port 8000
```

Window 3, call through the gateway as each role (set the same secret first):

```powershell
$env:GATEWAY_JWT_SECRET = "<the secret from window 2>"
python -m task2_gateway.demo_client --role viewer   # admin_reset_key -> -32001
python -m task2_gateway.demo_client --role admin    # admin_reset_key -> ok
```

Or mint a raw token for any HTTP client:

```powershell
python -m task2_gateway.mint_token --role viewer --sub alice --ttl 3600
```

| Variable | Default |
|----------|---------|
| `GATEWAY_JWT_SECRET` | required, at least 32 bytes |
| `GATEWAY_JWT_ISSUER` | `quilr-sa-exam` |
| `GATEWAY_JWT_AUDIENCE` | `quilr-mcp-gateway` |
| `GATEWAY_DOWNSTREAM_URL` | `http://127.0.0.1:8001/mcp` |
| `GATEWAY_MAX_BODY_BYTES` | `4194304` (4 MiB) |

### Task 3: Streaming PII guardrail

Window 1, mock LLM upstream (port 9100; speaks both provider formats):

```powershell
python -m llm_providers.mock_upstream --port 9100
```

Window 2, the gateway (port 8300):

```powershell
python -m task3_llm_stream --port 8300
```

Window 3, stream a response that contains PII and watch it arrive redacted:

```powershell
python -m task3_llm_stream.demo_client --model mock-pii
python -m task3_llm_stream.demo_client --model mock-pii --provider mock-anthropic
python -m task3_llm_stream.demo_client --prompt "hello there"
```

### Task 4: Rate limiting and failover

Window 1, the mock upstream as above (port 9100).

Window 2, create a tenant (prints its API key once), then start the gateway
(port 8400). The SQLite file is created in the current folder unless
`T4_DB_PATH` is set.

```powershell
python -m task4_router.admin add-tenant --tenant acme --tpm 50000
python -m task4_router --port 8400
```

Window 3, send a completion:

```powershell
$key = "<api_key printed by add-tenant>"
$body = '{"messages":[{"role":"user","content":"hello"}],"max_tokens":100}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8400/v1/completions `
  -Headers @{ Authorization = "Bearer $key" } -ContentType "application/json" -Body $body
```

To see failover, restart window 2 with the primary forced to fail, then repeat
the request. The response's `provider` becomes `mock-anthropic` and `failover`
becomes `true`:

```powershell
$env:MOCK_OPENAI_MODEL = "mock-429"         # or "mock-slow-5000" for the 3000 ms timeout
python -m task4_router --port 8400
```

Windows PowerShell 5.1's `Invoke-RestMethod` throws on 4xx/5xx responses; in
PowerShell 7 add `-SkipHttpErrorCheck` to see the standardized error body.

| Variable | Default |
|----------|---------|
| `T4_DB_PATH` | `task4_router.sqlite3` |
| `T4_PRIMARY_PROVIDER` / `T4_SECONDARY_PROVIDER` | `mock-openai` / `mock-anthropic` |
| `T4_PRIMARY_TIMEOUT_MS` / `T4_SECONDARY_TIMEOUT_MS` | `3000` / `3000` |
| `T4_FAILOVER_ON_5XX` | `false` |
| `T4_FAILOVER_ON_TRANSPORT_ERROR` | `false` |
| `T4_MAX_TOKENS_LIMIT` | `4096` |
| `T4_WINDOW_SECONDS` | `60` |

## 5. Using real providers (Tasks 3 and 4)

Provider names: `openai`, `anthropic`, `mock-openai`, `mock-anthropic`. Real
providers need a key and a model; no model name is defaulted, so set one your
account can use.

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_MODEL = "<a model your account can use>"
# $env:OPENAI_BASE_URL = "https://api.openai.com/v1"      # default; any OpenAI-compatible server works
# $env:OPENAI_MAX_TOKENS_FIELD = "max_tokens"             # for servers that expect the older field name

$env:ANTHROPIC_API_KEY = "..."
$env:ANTHROPIC_MODEL = "<a model your account can use>"

# Task 3: allow real providers
$env:T3_PROVIDERS = "openai,anthropic,mock-openai"
$env:T3_DEFAULT_PROVIDER = "anthropic"

# Task 4: real primary, real secondary
$env:T4_PRIMARY_PROVIDER = "openai"
$env:T4_SECONDARY_PROVIDER = "anthropic"
```

Mock model names choose mock behaviors: `mock-echo`, `mock-pii`, `mock-429`,
`mock-500`, `mock-slow-<ms>`, `mock-midstream-error` (combinable, e.g.
`mock-pii-slow-200`). Task 3 clients can pass `"model"` per request; Task 4 uses
`MOCK_OPENAI_MODEL` / `MOCK_ANTHROPIC_MODEL` (or `MOCK_MODEL` for both).

## Troubleshooting

* **`ModuleNotFoundError: task1_mcp_server`** – run from the repository root with
  the environment's Python, or re-run `pip install -e ".[dev]"`.
* **Port already in use** – pass a different `--port` (and update URLs to match).
* **`GATEWAY_JWT_SECRET is not set`** – environment variables set with `$env:` last
  only for that PowerShell window; set it in each window that needs it.
