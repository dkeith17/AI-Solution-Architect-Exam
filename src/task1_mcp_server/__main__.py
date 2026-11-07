"""Entry point: ``python -m task1_mcp_server`` (stdio transport).

stdout carries JSON-RPC only. Every log line goes to stderr.
"""

from __future__ import annotations

import logging
import sys

import anyio

from mcp.server.stdio import stdio_server

from . import config as cfg
from .server import build_from_environment


def _configure_logging() -> None:
    # Explicit stream=sys.stderr: the default would also be stderr, but stating it
    # keeps a future refactor from moving logs onto the protocol stream.
    logging.basicConfig(
        stream=sys.stderr,
        level=cfg.log_level(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.captureWarnings(True)


def _harden_stray_stdout() -> None:
    """Make stray ``print`` output leave Python's buffer immediately.

    While serving, the SDK's stdio transport points fd 1 at stderr. Python
    block-buffers ``sys.stdout`` when it is a pipe, so without this a stray print
    could sit in the buffer and be flushed at exit, after fd 1 is restored, onto the
    real protocol stream. The same ``sys.stdout`` object is kept (only its buffering
    changes), so the transport's own descriptor handling is unaffected.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)


async def _serve() -> None:
    server = build_from_environment()
    logging.getLogger("task1.main").info("starting stdio server")
    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            # Flush any partial stray output while fd 1 still points at stderr.
            sys.stdout.flush()


def main() -> None:
    _configure_logging()
    _harden_stray_stdout()
    anyio.run(_serve)


if __name__ == "__main__":
    main()
