"""Release smokes must not inherit the tiny synthetic task's execution limits."""

from pathlib import Path

import pytest
import yaml
from harbor.models.job.config import JobConfig


CONFIGS = Path(__file__).resolve().parents[2] / "configs"


@pytest.mark.parametrize(
    "filename,model",
    [
        (
            "harbor_job_orca_bench_agent_o11y_sonnet.yaml",
            "anthropic/claude-sonnet-5-5",
        ),
        (
            "harbor_job_orca_bench_agent_o11y_fireworks_qwen3p8_max.yaml",
            "fireworks_ai/accounts/fireworks/models/qwen3p8-max",
        ),
    ],
)
def test_release_smoke_scope(filename, model):
    raw = yaml.safe_load((CONFIGS / filename).read_text())
    config = JobConfig.model_validate(raw)
    assert config.n_concurrent_trials == config.n_attempts == 1
    assert raw["tasks"] == [{"name": "orca-bench/05216f608f940a48", "ref": "latest"}]
    assert len(config.agents) == 1
    agent = config.agents[0]
    assert agent.import_path == "orca_model_calls.terminus:AgentO11yTerminus2"
    assert agent.model_name == model
    assert "max_turns" not in agent.kwargs
    assert agent.kwargs["capture_byte_limit"] == 67108864
    assert agent.kwargs["capture_call_byte_limit"] == 4194304
    assert agent.kwargs["llm_kwargs"]["max_tokens"] == 16384
    assert "environment" not in raw
    assert "timeout_multiplier" not in raw


def test_fireworks_options_are_not_anthropic_options():
    raw = yaml.safe_load(
        (CONFIGS / "harbor_job_orca_bench_agent_o11y_fireworks_qwen3p8_max.yaml").read_text()
    )
    kwargs = raw["agents"][0]["kwargs"]
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["temperature"] == 1
    assert "thinking" not in kwargs["llm_kwargs"]
    assert "api_key" not in kwargs["llm_kwargs"]
    assert "api_base" not in kwargs["llm_kwargs"]
