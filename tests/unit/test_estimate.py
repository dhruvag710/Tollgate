from tollgate.ratelimit.estimate import (
    count_tokens,
    encoding_for,
    estimate_prompt_tokens,
    generated_text,
    warm_up,
)

MODEL = "gpt-4o-mini"


def test_encodings() -> None:
    warm_up([MODEL, "llama3.1:8b"])
    assert encoding_for(MODEL).name == "o200k_base"
    assert encoding_for("llama3.1:8b").name == "o200k_base"  # unknown: fallback
    assert encoding_for("gpt-4").name == "cl100k_base"


def test_count_tokens() -> None:
    assert count_tokens("hello world", MODEL) == 2
    assert count_tokens("", MODEL) == 0
    # Special-token text in user content is counted as text, not rejected.
    assert count_tokens("<|endoftext|>", MODEL) > 1


def test_prompt_framing() -> None:
    # reply primer 3 + per-message 3 + "user" 1 + "hello world" 2
    assert estimate_prompt_tokens([{"role": "user", "content": "hello world"}], MODEL) == 9


def test_prompt_parts_name_and_tool_calls() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}
    text_part = {"type": "text", "text": "hello world"}
    with_parts = [{"role": "user", "name": "bob", "content": [text_part, image]}]
    plain = [{"role": "user", "content": "hello world"}]
    # The name adds its own tokens plus 1; the image adds nothing (not estimable).
    assert estimate_prompt_tokens(with_parts, MODEL) == (
        estimate_prompt_tokens(plain, MODEL) + count_tokens("bob", MODEL) + 1
    )
    calls = [{"role": "assistant", "content": None, "tool_calls": [{"type": "function"}]}]
    assert estimate_prompt_tokens(calls, MODEL) > 3 + 3
    # Shapes Tollgate doesn't model are counted as their JSON, never dropped.
    for odd in ({"a": "b"}, ["loose string part"]):
        assert estimate_prompt_tokens([{"role": "user", "content": odd}], MODEL) > 3 + 3 + 1


def test_generated_text() -> None:
    message = {
        "role": "assistant",
        "content": "a",
        "refusal": None,
        "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}, "junk"],
    }
    assert generated_text(message) == "af{}"
    assert generated_text(None) == ""
    assert generated_text({"tool_calls": "junk"}) == ""
