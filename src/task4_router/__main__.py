"""Run the Task 4 gateway: ``python -m task4_router --port 8400``."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import Settings, create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LLM gateway with rate limiting and model failover")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8400)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
