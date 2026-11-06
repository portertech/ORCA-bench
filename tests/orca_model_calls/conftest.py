from datetime import datetime, timezone

import pytest

from orca_model_calls.capture import Recorder


@pytest.fixture
def response():
    return {
        "id": "synthetic-response",
        "model": "synthetic-runtime",
        "created": 1700000000,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "synthetic answer",
                    "reasoning_content": "synthetic reasoning",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


@pytest.fixture
def record(response):
    def add(recorder=None, **overrides):
        recorder = recorder or Recorder()
        options = dict(
            requested_model="synthetic/requested",
            provider="synthetic",
            reasoning_effort="medium",
            messages=[{"role": "user", "content": "synthetic task"}],
            response=response,
            completed_at=datetime(2023, 11, 14, tzinfo=timezone.utc),
        )
        options.update(overrides)
        identity = recorder.begin()
        recorder.record(identity, **options)
        return recorder

    return add
