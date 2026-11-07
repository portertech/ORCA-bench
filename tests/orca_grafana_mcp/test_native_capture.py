import copy
from datetime import datetime, timezone

import pytest

from orca_model_calls.atif import envelope, validate
from orca_model_calls.v2 import NativeRecorder


TOOLS = [{"type": "function", "function": {"name": "grafana_query", "description": "Synthetic query",
           "parameters": {"type": "object", "properties": {"percentile": {"type": "number"}}}}}]


def response():
    return {"id": "synthetic-response", "model": "synthetic-model", "created": 1700000000,
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "reasoning_content": "Synthetic reasoning",
                "tool_calls": [{"id": "tool-1", "type": "function", "function": {
                    "name": "grafana_query", "arguments": '{"percentile":0.99}'}}]}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}


def artifact(recorder):
    result = envelope(recorder, "synthetic-session")
    result["agent"] = {"name": "orca-grafana-mcp", "version": "test"}
    return result


def record(recorder, raw=None, messages=None):
    recorder.record_native(recorder.begin(), model="openai/synthetic", options={"max_tokens": 32, "temperature": 0.5, "tool_choice": "auto"},
                           tools=TOOLS, messages=messages or [{"role": "user", "content": "Synthetic task"}],
                           response=raw or response())


def test_native_capture_is_immutable_and_preserves_arguments():
    r = NativeRecorder()
    raw = response()
    record(r, raw)
    raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "changed"
    r.finalized = True
    a = validate(artifact(r))
    assert a["extra"]["llmspec_model_calls"]["calls"][0]["response"]["message"]["tool_calls"][0]["function"]["arguments"] == '{"percentile":0.99}'
    assert r.successful_responses == 1


@pytest.mark.parametrize("mutate", [
    lambda a: a["extra"]["llmspec_model_calls"].update(successful_responses=2),
    lambda a: a["extra"]["llmspec_model_calls"].update(finalized=True, pending_requests=1),
    lambda a: a["extra"]["llmspec_model_calls"]["calls"][0]["messages"].append({"role": "tool", "tool_call_id": "orphan", "content": "bad"}),
])
def test_reject_inconsistent_capture(mutate):
    r = NativeRecorder(); record(r)
    a = artifact(r); mutate(a)
    with pytest.raises(ValueError):
        validate(a)


def test_unsupported_message_is_accounted_not_coerced():
    r = NativeRecorder(); raw = response()
    raw["choices"][0]["message"]["audio"] = {"data": "unsupported"}
    record(r, raw)
    assert r.rejected_calls == 1 and r.successful_responses == 1
    validate(artifact(r))


def test_whole_call_drop():
    r = NativeRecorder(call_byte_limit=100); record(r)
    assert r.dropped_calls == 1 and r.truncated and not r.extension()["calls"]
    validate(artifact(r))


def test_native_history_roundtrip_and_malformed_arguments():
    r = NativeRecorder(); raw = response()
    raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{invalid JSON"
    record(r, raw)
    history = [{"role": "user", "content": "Synthetic task"}, raw["choices"][0]["message"],
               {"role": "tool", "tool_call_id": "tool-1", "content": "Argument parse error"}]
    raw2 = response(); raw2["id"] = "synthetic-response-2"
    raw2["choices"][0]["message"] = {"role": "assistant", "content": "Synthetic answer"}
    raw2["choices"][0]["finish_reason"] = "stop"
    record(r, raw2, history)
    validate(artifact(r))
    assert len(r.extension()["calls"]) == 2
