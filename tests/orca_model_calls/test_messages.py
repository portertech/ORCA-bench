import pytest

from orca_model_calls.messages import (
    Unrepresentable,
    messages_to_wire,
    response_to_wire,
)
from datetime import datetime, timezone


def test_conversion():
    cache = {"type": "ephemeral"}
    messages = messages_to_wire(
        [
            {"role": "system", "content": "system", "cache_control": cache},
            {
                "role": "user",
                "content": [{"type": "text", "text": "task", "cache_control": cache}],
            },
            {"role": "assistant", "content": "answer", "reasoning_content": "reason"},
        ]
    )
    assert messages[0]["content"] == "system"
    assert messages[0]["providerOptions"]["cache_control"] == cache
    assert messages[1]["content"][0]["providerOptions"]["cache_control"] == cache
    assert messages[2]["content"] == [
        {"type": "reasoning", "text": "reason"},
        {"type": "text", "text": "answer"},
    ]


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "x"},
        {"role": "user", "content": [{"type": "image_url", "image_url": "x"}]},
        {"role": "user", "content": [{"type": "text", "text": 1}]},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "x", "tool_calls": [{}]},
        {"role": "system", "content": []},
        {"role": "user", "content": "x", "unknown": True},
    ],
)
def test_closed_mapping(message):
    with pytest.raises(Unrepresentable):
        messages_to_wire([message])


@pytest.mark.parametrize(
    "finish,unified",
    [("stop", "stop"), ("length", "length"), ("new-reason", "unknown")],
)
def test_finish_and_empty_text(response, finish, unified):
    response["choices"][0]["finish_reason"] = finish
    response["choices"][0]["message"]["content"] = ""
    mapped = response_to_wire(response, datetime.now(timezone.utc))
    assert mapped["finish_reason"] == {"raw": finish, "unified": unified}
    assert mapped["message"]["content"][-1]["text"] == ""


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, {}),
        ({"completion_tokens": 2}, {}),
        (
            {"prompt_tokens": 4, "prompt_tokens_details": {"cached_tokens": 1}},
            {"total": 4},
        ),
    ],
)
def test_partial_usage(response, usage, expected):
    response["usage"] = usage
    assert (
        response_to_wire(response, datetime.now(timezone.utc))["usage"]["input_tokens"]
        == expected
    )
