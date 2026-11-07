import asyncio
from datetime import datetime, timezone

import litellm
import pytest

from orca_grafana_mcp.llm import complete
from orca_model_calls.v2 import NativeRecorder
from test_native_capture import response


def raw(identifier):
    result = response()
    result["id"] = identifier
    result["choices"][0]["message"] = {
        "role": "assistant",
        "content": "Synthetic final",
    }
    result["choices"][0]["finish_reason"] = "stop"
    return litellm.ModelResponse(**result)


async def notify(callback, result):
    now = datetime.now(timezone.utc)
    await callback({"custom_llm_provider": "openai"}, result, now, now)


def test_duplicate_record_is_idempotent_and_drops_keep_usage():
    recorder = NativeRecorder(call_byte_limit=100)
    ident = recorder.begin()
    args = dict(
        model="openai/synthetic",
        options={"max_tokens": 32, "tool_choice": "auto"},
        messages=[{"role": "user", "content": "Synthetic"}],
        tools=[],
        response=raw("synthetic-response"),
    )
    recorder.record_native(ident, **args)
    recorder.record_native(ident, **args)
    assert recorder.successful_responses == 1 and recorder.dropped_calls == 1
    assert recorder.usage_totals["n_input_tokens"] == 12
    assert recorder.usage_totals["n_output_tokens"] == 5


def test_multiple_successes_fail_closed_but_keep_both_responses():
    async def scenario():
        recorder = NativeRecorder()

        async def completion(**kwargs):
            first, second = raw("synthetic-first"), raw("synthetic-second")
            callback = kwargs["success_callback"][0]
            await notify(callback, first)
            await notify(callback, second)
            return second

        with pytest.raises(ValueError, match="incomplete"):
            await complete(
                recorder,
                "openai/synthetic",
                [],
                [],
                {"max_tokens": 32, "tool_choice": "auto"},
                lambda: None,
                completion=completion,
            )
        assert (
            recorder.successful_responses == 2
            and len(recorder.extension()["calls"]) == 2
        )
        assert recorder.uncertain_requests > 0

    asyncio.run(scenario())


def test_late_callback_cannot_modify_closed_or_next_execution():
    async def scenario():
        first, second = NativeRecorder(), NativeRecorder()
        late = None

        async def delayed(**kwargs):
            nonlocal late
            result = raw("synthetic-late")

            async def callback():
                await asyncio.sleep(0.05)
                await notify(kwargs["success_callback"][0], result)

            late = asyncio.create_task(callback())
            return result

        with pytest.raises(ValueError, match="incomplete"):
            await complete(
                first,
                "openai/synthetic",
                [],
                [],
                {"max_tokens": 32, "tool_choice": "auto"},
                lambda: None,
                completion=delayed,
                callback_timeout=0.001,
            )
        snapshot = first.extension()

        async def ontime(**kwargs):
            result = raw("synthetic-ontime")
            await notify(kwargs["success_callback"][0], result)
            return result

        await complete(
            second,
            "openai/synthetic",
            [],
            [],
            {"max_tokens": 32, "tool_choice": "auto"},
            lambda: None,
            completion=ontime,
        )
        await late
        assert first.extension() == snapshot
        assert second.successful_responses == 1 and not second.uncertain_requests

    asyncio.run(scenario())
