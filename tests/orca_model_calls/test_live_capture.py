"""Opt-in paid-provider boundary smoke; never writes real artifacts into Git."""

import asyncio
import hashlib
import os

import pytest

from orca_model_calls.atif import envelope, validate
from orca_model_calls.capture import Recorder, encode
from orca_model_calls.lite_llm import CapturingLiteLLM


@pytest.mark.skipif(
    not os.getenv("ORCA_LIVE_CAPTURE_MODEL"),
    reason="set ORCA_LIVE_CAPTURE_MODEL explicitly to enable a paid-provider smoke",
)
def test_live_boundary():
    async def run():
        recorder = Recorder()
        llm = CapturingLiteLLM(
            os.environ["ORCA_LIVE_CAPTURE_MODEL"],
            recorder=recorder,
            api_base=os.getenv("ORCA_LIVE_CAPTURE_API_BASE"),
            max_tokens=32,
            callback_timeout=10,
        )
        await llm.call(
            "Reply with exactly OK. This is a synthetic capture conformance test."
        )
        artifact = validate(envelope(recorder, "live-boundary-smoke"))
        extension = artifact["extra"]["llmspec_model_calls"]
        assert not extension["truncated"]
        assert extension["dropped_calls"] == extension["rejected_calls"] == 0
        assert len(extension["calls"]) == 1
        call = extension["calls"][0]
        assert call["response"]["finish_reason"]["unified"] == "stop"
        raw = encode(artifact)
        for name in ("OPENAI_API_KEY", "GRADIENT_API_KEY"):
            secret = os.getenv(name)
            if secret:
                assert secret.encode() not in raw
        print(
            f"live capture: calls=1 bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"
        )

    asyncio.run(run())
