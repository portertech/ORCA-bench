import asyncio
import hashlib
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from orca_grafana_mcp.agent import GrafanaMCPAgent
from harbor.models.agent.context import AgentContext


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    (root / "wheels").mkdir(mode=0o700)
    (root / "mcp-grafana").write_bytes(b"synthetic executable")
    (root / "LICENSE").write_text("Synthetic notice")
    (root / "wheels/synthetic.whl").write_text("Synthetic wheel, never executed")
    lock = Path("tools/grafana-mcp-runtime/requirements.lock").read_bytes()
    (root / "requirements.lock").write_bytes(lock)
    manifest = {}
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
            manifest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(
        "orca_grafana_mcp.agent.SERVER_BINARY_SHA256", manifest["mcp-grafana"]
    )
    return root


class Environment:
    def __init__(self):
        self.uploads = []
        self.commands = []

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return SimpleNamespace(return_code=0)

    async def upload_dir(self, source, target):
        # Docker uploads may be root-owned. Every directory/file in the public
        # runtime/source payload must be traversable/readable by the agent user.
        for path in [Path(source), *Path(source).rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o004
            if path.is_dir():
                assert mode & 0o001
        self.uploads.append(target)


def test_private_umask_payload_is_usable_by_non_root(bundle, tmp_path):
    agent = GrafanaMCPAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5-5",
        runtime_dir=bundle,
        thinking={"type": "between_tools"},
    )
    assert agent.options["allowed_openai_params"] == [
        "tools",
        "tool_choice",
        "thinking",
    ]
    env = Environment()
    asyncio.run(agent.setup(env))
    assert len(env.uploads) == 2
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "mcp-grafana").stat().st_mode) == 0o600
    assert (
        "--no-index" in env.commands[-1][0]
        and "--require-hashes" in env.commands[-1][0]
    )


def test_tampered_runtime_is_rejected_before_upload(bundle, tmp_path):
    (bundle / "LICENSE").write_text("Changed")
    agent = GrafanaMCPAgent(
        logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", runtime_dir=bundle
    )
    env = Environment()
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(agent.setup(env))
    assert not env.uploads


def test_backfill_unknown_cost_and_usage(tmp_path):
    (tmp_path / "metrics.json").write_text(
        json.dumps(
            {
                "n_input_tokens": 12,
                "n_output_tokens": 5,
                "n_cache_tokens": 0,
                "cost_usd": None,
            }
        )
    )
    agent = GrafanaMCPAgent(
        logs_dir=tmp_path,
        model_name="fireworks_ai/accounts/fireworks/models/qwen3p8-max",
        reasoning_effort="medium",
        temperature=1,
    )
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert (
        context.n_input_tokens == 12
        and context.n_output_tokens == 5
        and context.cost_usd is None
    )
    assert agent.options["allowed_openai_params"] == [
        "tools",
        "tool_choice",
        "reasoning_effort",
    ]
