import asyncio
from datetime import datetime, timezone

import pytest
from harbor.llms.base import OutputLengthExceededError
from harbor.llms.lite_llm import LiteLLM
from litellm import ModelResponse

from orca_model_calls.capture import Recorder
from orca_model_calls.lite_llm import CapturingLiteLLM


def test_missing_and_late_callback_accounted(response, monkeypatch):
    async def run():
        raw = ModelResponse(**response)
        tasks = []

        async def fake_call(self, *args, **kwargs):
            self._extract_usage_info(raw)

            async def late():
                await asyncio.sleep(0.03)
                await kwargs["success_callback"][-1](
                    {
                        "messages": [{"role": "user", "content": "synthetic"}],
                        "custom_llm_provider": "openai",
                    },
                    raw,
                    None,
                    datetime.now(timezone.utc),
                )

            tasks.append(asyncio.create_task(late()))
            return "synthetic result"

        monkeypatch.setattr(LiteLLM, "call", fake_call)
        recorder = Recorder()
        llm = CapturingLiteLLM(
            "openai/gpt-4o-mini", recorder=recorder, callback_timeout=0.005
        )
        await llm.call("synthetic")
        assert recorder.extension()["rejected_calls"] == 1
        assert recorder.extension()["calls"] == []
        await asyncio.gather(*tasks)
        assert recorder.extension()["calls"] == []
        assert recorder.extension()["rejected_calls"] == 1

    asyncio.run(run())


def test_failed_provider_not_completion(monkeypatch):
    async def fake_call(self, *args, **kwargs):
        raise RuntimeError("synthetic provider failure without response")

    monkeypatch.setattr(LiteLLM, "call", fake_call)
    recorder = Recorder()
    llm = CapturingLiteLLM(
        "openai/gpt-4o-mini", recorder=recorder, callback_timeout=0.01
    )
    with pytest.raises(RuntimeError):
        asyncio.run(llm.call("synthetic"))
    assert recorder.extension()["calls"] == []
    assert recorder.extension()["rejected_calls"] == 0


def test_length_preserved_before_harbor_exception(response, monkeypatch):
    response["choices"][0]["finish_reason"] = "length"
    raw = ModelResponse(**response)

    async def fake_call(self, *args, **kwargs):
        self._extract_usage_info(raw)
        await kwargs["success_callback"][-1](
            {
                "messages": [{"role": "user", "content": "synthetic"}],
                "custom_llm_provider": "openai",
            },
            raw,
            None,
            None,
        )
        raise OutputLengthExceededError(
            "synthetic length", truncated_response="synthetic"
        )

    monkeypatch.setattr(LiteLLM, "call", fake_call)
    recorder = Recorder()
    llm = CapturingLiteLLM("openai/gpt-4o-mini", recorder=recorder)
    with pytest.raises(OutputLengthExceededError):
        asyncio.run(llm.call("synthetic"))
    ext = recorder.extension()
    assert ext["rejected_calls"] == 0
    assert ext["calls"][0]["response"]["finish_reason"]["unified"] == "length"


def test_retried_successes_keep_order_and_missing_callback(response, monkeypatch):
    first = ModelResponse(**response)
    response["id"] = "second-response"
    second = ModelResponse(**response)

    async def fake_call(self, *args, **kwargs):
        self._extract_usage_info(first)
        self._extract_usage_info(second)
        # Earlier successful response has no callback. Later success must not
        # falsely satisfy the wait, and ordinal 1 must be explicitly rejected.
        await kwargs["success_callback"][-1](
            {
                "messages": [{"role": "user", "content": "synthetic"}],
                "custom_llm_provider": "openai",
            },
            second,
            None,
            None,
        )
        return "synthetic result"

    monkeypatch.setattr(LiteLLM, "call", fake_call)
    recorder = Recorder()
    llm = CapturingLiteLLM(
        "openai/gpt-4o-mini", recorder=recorder, callback_timeout=0.001
    )
    asyncio.run(llm.call("synthetic"))
    ext = recorder.extension()
    assert ext["rejected_calls"] == 1
    assert len(ext["calls"]) == 1
    assert ext["calls"][0]["ordinal"] == 2
