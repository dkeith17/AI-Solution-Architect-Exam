"""Provider configuration from environment variables.

Provider names and the variables each one reads:

  openai          OPENAI_API_KEY (required), OPENAI_MODEL (required),
                  OPENAI_BASE_URL (default https://api.openai.com/v1),
                  OPENAI_MAX_TOKENS_FIELD (default max_completion_tokens; some
                  OpenAI-compatible servers expect max_tokens)
  anthropic       ANTHROPIC_API_KEY (required), ANTHROPIC_MODEL (required),
                  ANTHROPIC_BASE_URL (default https://api.anthropic.com)
  mock-openai     MOCK_UPSTREAM_URL (default http://127.0.0.1:9100),
                  MOCK_OPENAI_MODEL, else MOCK_MODEL, else mock-echo
  mock-anthropic  MOCK_UPSTREAM_URL, MOCK_ANTHROPIC_MODEL, else MOCK_MODEL, else mock-echo
                  (mock model names select behaviors; see llm_providers.mock_wire)

No model names are defaulted for real providers: set the one your account can use.
"""

from __future__ import annotations

import os

from .adapters import ProviderConfig

PROVIDER_NAMES = ("openai", "anthropic", "mock-openai", "mock-anthropic")


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise RuntimeError(f"environment variable {key} is required for this provider")
    return value


def provider_config(name: str, environ: dict[str, str] | None = None) -> ProviderConfig:
    env = os.environ if environ is None else environ
    if name == "openai":
        return ProviderConfig(
            name="openai", kind="openai",
            base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=_require(env, "OPENAI_API_KEY"),
            model=_require(env, "OPENAI_MODEL"),
            max_tokens_field=env.get("OPENAI_MAX_TOKENS_FIELD", "max_completion_tokens"),
        )
    if name == "anthropic":
        return ProviderConfig(
            name="anthropic", kind="anthropic",
            base_url=env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            api_key=_require(env, "ANTHROPIC_API_KEY"),
            model=_require(env, "ANTHROPIC_MODEL"),
        )
    mock_base = env.get("MOCK_UPSTREAM_URL", "http://127.0.0.1:9100").rstrip("/")
    shared = env.get("MOCK_MODEL", "mock-echo")
    if name == "mock-openai":
        return ProviderConfig(name="mock-openai", kind="openai", base_url=mock_base + "/openai/v1",
                              api_key="mock-key", model=env.get("MOCK_OPENAI_MODEL", shared))
    if name == "mock-anthropic":
        return ProviderConfig(name="mock-anthropic", kind="anthropic", base_url=mock_base + "/anthropic",
                              api_key="mock-key", model=env.get("MOCK_ANTHROPIC_MODEL", shared))
    raise RuntimeError(f"unknown provider {name!r}; expected one of {PROVIDER_NAMES}")
