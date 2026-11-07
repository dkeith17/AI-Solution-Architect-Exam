"""Run the gateway: ``python -m task2_gateway --port 8000``."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import GatewaySettings, create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MCP security gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(GatewaySettings.from_env())
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
