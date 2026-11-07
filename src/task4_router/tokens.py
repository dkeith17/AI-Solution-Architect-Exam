"""Token estimation for admission control.

Admission happens before the provider answers, so the limiter reserves an upper
bound: estimated prompt tokens + ``max_tokens`` (the most the model may generate).
The prompt estimate uses ~4 characters per token plus a small per-message
overhead. It is a heuristic, not a tokenizer; the reservation is reconciled to the
provider-reported usage after the call, so the estimate only affects admission.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 4
PER_MESSAGE_OVERHEAD = 4
PER_REQUEST_OVERHEAD = 2


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    total = PER_REQUEST_OVERHEAD
    for m in messages:
        total += PER_MESSAGE_OVERHEAD + math.ceil(len(m.get("content", "")) / CHARS_PER_TOKEN)
    return total


def reservation_tokens(messages: list[dict[str, str]], max_tokens: int) -> int:
    return estimate_prompt_tokens(messages) + max_tokens
