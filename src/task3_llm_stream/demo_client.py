"""Stream a redacted response from the Task 3 gateway and print it as it arrives.

    python -m task3_llm_stream.demo_client --model mock-pii
    python -m task3_llm_stream.demo_client --provider mock-anthropic --prompt "hello"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx2

from llm_providers.sse import SSEParser


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8300/v1/chat/stream")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", default="Tell me about the account.")
    args = parser.parse_args(argv)

    body = {"messages": [{"role": "user", "content": args.prompt}], "max_tokens": 256}
    if args.provider:
        body["provider"] = args.provider
    if args.model:
        body["model"] = args.model

    start = time.perf_counter()
    first = None
    sse = SSEParser()
    with httpx2.Client(timeout=60) as client:
        with client.stream("POST", args.url, json=body) as resp:
            if resp.status_code != 200:
                print(f"HTTP {resp.status_code}: {resp.read().decode()}", file=sys.stderr)
                sys.exit(1)
            for chunk in resp.iter_bytes():
                for ev in sse.feed(chunk):
                    data = json.loads(ev.data)
                    if ev.event == "delta":
                        if first is None:
                            first = time.perf_counter() - start
                        print(data["text"], end="", flush=True)
                    else:
                        print(f"\n[{ev.event}] {json.dumps(data)}")
    if first is not None:
        print(f"time to first redacted token: {first * 1000:.0f} ms", file=sys.stderr)


if __name__ == "__main__":
    main()
