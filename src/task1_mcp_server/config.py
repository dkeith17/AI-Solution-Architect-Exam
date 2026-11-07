"""Configuration loading for the Task 1 server.

Sources, later ones overriding earlier ones:
  1. built-in defaults (every injection category rejected, 4096-char limit)
  2. a TOML file named by the QUILR_T1_CONFIG environment variable
  3. QUILR_T1_DEFAULT_ACTION=reject|allow (overrides [sanitizer].default_action)
Logging level comes from QUILR_T1_LOG_LEVEL (default INFO). Logs always go to stderr.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path

from .sanitizer import Action, SanitizerConfig

ENV_CONFIG_PATH = "QUILR_T1_CONFIG"
ENV_DEFAULT_ACTION = "QUILR_T1_DEFAULT_ACTION"
ENV_LOG_LEVEL = "QUILR_T1_LOG_LEVEL"
ENV_FAULT_INJECT = "QUILR_T1_FAULT_INJECT_STDOUT"


def load_sanitizer_config(environ: dict[str, str] | None = None) -> SanitizerConfig:
    env = os.environ if environ is None else environ
    cfg = SanitizerConfig()
    path = env.get(ENV_CONFIG_PATH)
    if path:
        with Path(path).open("rb") as fh:
            cfg = SanitizerConfig.from_mapping(tomllib.load(fh))
    override = env.get(ENV_DEFAULT_ACTION)
    if override:
        cfg = dataclasses.replace(cfg, default_action=Action(override.strip().lower()))
    return cfg


def log_level(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(ENV_LOG_LEVEL, "INFO").upper()


def fault_injection_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_FAULT_INJECT) == "1"
