from task4_router.tokens import estimate_prompt_tokens, reservation_tokens


def test_estimate_is_monotonic_and_includes_overhead() -> None:
    short = estimate_prompt_tokens([{"role": "user", "content": "hi"}])
    long = estimate_prompt_tokens([{"role": "user", "content": "hi" * 400}])
    assert 0 < short < long
    assert estimate_prompt_tokens([{"role": "user", "content": "x" * 400}]) == 2 + 4 + 100


def test_reservation_adds_max_tokens() -> None:
    msgs = [{"role": "user", "content": "abcd"}]
    assert reservation_tokens(msgs, 500) == estimate_prompt_tokens(msgs) + 500
