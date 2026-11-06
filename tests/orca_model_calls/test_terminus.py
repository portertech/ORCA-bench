import asyncio

import pytest
from harbor.agents.terminus_2.terminus_2 import Terminus2

from orca_model_calls.atif import loads, validate
from orca_model_calls.terminus import AgentO11yTerminus2


def test_agent_lifecycle_and_finally(tmp_path, monkeypatch):
    async def failing_run(self, *args):
        self._reset_per_run_state()
        (self.logs_dir / "trajectory.json").write_text(
            '{"steps": [], "final_metrics": {}}'
        )
        raise RuntimeError("synthetic run failure")

    monkeypatch.setattr(Terminus2, "run", failing_run)
    agent = AgentO11yTerminus2(logs_dir=tmp_path, model_name="openai/gpt-4o-mini")
    old = agent.recorder.execution_generation_id
    with pytest.raises(RuntimeError):
        asyncio.run(agent.run("synthetic", None, None))
    path = tmp_path / "llmspec-trajectory.atif.json"
    artifact = validate(loads(path.read_bytes()))
    assert artifact["extra"]["llmspec_model_calls"]["execution_generation_id"] != old
    assert (tmp_path / "trajectory.json").exists()
    with pytest.raises(ValueError, match="multi-step/resume"):
        asyncio.run(agent.run("synthetic", None, None))
    with pytest.raises(ValueError, match="resume"):
        asyncio.run(agent.resume())


@pytest.mark.parametrize(
    "options",
    [
        {"use_responses_api": True},
        {"llm_backend": "tinker"},
        {"trajectory_config": {"linear_history": True}},
    ],
)
def test_unsupported_modes(tmp_path, options):
    with pytest.raises(ValueError):
        AgentO11yTerminus2(
            logs_dir=tmp_path, model_name="openai/gpt-4o-mini", **options
        )
