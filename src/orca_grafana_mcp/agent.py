"""Harbor 0.20 adapter for the isolated native-MCP worker."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile

from harbor.agents.base import BaseAgent

from . import NAME, VERSION

WORK = "/agent/grafana-mcp"
LOGS = "/logs/agent"
SERVER_BINARY_SHA256 = (
    "83f9e5f5419359e987add4ccc84e5a82953f59e16c7df48768a42d1f33e0a424"
)


class GrafanaMCPAgent(BaseAgent):
    SUPPORTS_ATIF = True

    def __init__(
        self,
        *args,
        runtime_dir=".cache/grafana-mcp-runtime/linux-amd64",
        max_turns=100,
        max_tokens=16384,
        temperature=None,
        reasoning_effort=None,
        thinking=None,
        capture_byte_limit=67108864,
        capture_call_byte_limit=4194304,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if not self.model_name or self.model_name.split("/", 1)[0] not in {
            "anthropic",
            "fireworks_ai",
        }:
            raise ValueError(
                "native MCP agent currently supports anthropic and fireworks_ai"
            )
        if self.mcp_servers:
            raise ValueError(
                "custom MCP servers are not supported; this agent manages one task-local stdio server"
            )
        if type(max_turns) is not int or not 1 <= max_turns <= 200:
            raise ValueError("max_turns must be between 1 and 200")
        self.runtime_dir = Path(runtime_dir).resolve()
        self.settings = {
            "max_turns": max_turns,
            "capture_byte_limit": capture_byte_limit,
            "capture_call_byte_limit": capture_call_byte_limit,
        }
        self.options = {"max_tokens": max_tokens, "tool_choice": "auto"}
        for key, value in (
            ("temperature", temperature),
            ("reasoning_effort", reasoning_effort),
            ("thinking", thinking),
        ):
            if value is not None:
                self.options[key] = value
        # The pinned bundled model catalog predates these model names. Permit
        # only explicitly configured, provider-preflighted reasoning controls;
        # never silently drop them or download mutable pricing metadata in trials.
        permitted = ["tools", "tool_choice"] + [
            name for name in ("thinking", "reasoning_effort") if name in self.options
        ]
        if permitted:
            self.options["allowed_openai_params"] = permitted
        from orca_model_calls.v2.validation import validator
        import jsonschema

        schema = validator().schema["$defs"]["call"]["properties"]["request_options"]
        jsonschema.validate(self.options, schema)

    @staticmethod
    def name():
        return NAME

    def version(self):
        return VERSION

    async def setup(self, environment):
        manifest = json.loads((self.runtime_dir / "manifest.json").read_text())
        required = {"mcp-grafana", "LICENSE", "requirements.lock"}
        if not required <= manifest.keys() or not any(
            name.startswith("wheels/") for name in manifest
        ):
            raise ValueError(
                "prepare a complete worker bundle, not a server-only bundle"
            )
        binary_hash = hashlib.sha256(
            (self.runtime_dir / "mcp-grafana").read_bytes()
        ).hexdigest()
        if binary_hash != SERVER_BINARY_SHA256:
            raise ValueError(
                "runtime binary is not the pinned Linux AMD64 mcp-grafana release"
            )
        expected_lock = (
            Path(__file__).resolve().parents[2]
            / "tools/grafana-mcp-runtime/requirements.lock"
        )
        # In an installed wheel the operator-provided runtime still carries hashes;
        # source checkouts additionally enforce correspondence to the tracked lock.
        if (
            expected_lock.exists()
            and expected_lock.read_bytes()
            != (self.runtime_dir / "requirements.lock").read_bytes()
        ):
            raise ValueError("runtime lock differs from repository")
        for name, expected in manifest.items():
            path = (self.runtime_dir / name).resolve()
            if (
                not path.is_relative_to(self.runtime_dir)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("runtime manifest mismatch")
        result = await environment.exec(f"mkdir -p {WORK} {LOGS}")
        if result.return_code != 0:
            raise RuntimeError("agent workspace is not writable")
        with tempfile.TemporaryDirectory() as temp:
            # Docker upload preserves source modes but not agent ownership. Build a
            # readable public payload without changing the operator's cache modes.
            runtime = Path(temp) / "runtime"
            shutil.copytree(self.runtime_dir, runtime)
            for path in [runtime, *runtime.rglob("*")]:
                path.chmod(
                    0o755 if path.is_dir() or path.name == "mcp-grafana" else 0o644
                )
            await environment.upload_dir(runtime, f"{WORK}/runtime")
            source = Path(temp) / "src"
            source.mkdir(mode=0o755)
            source.chmod(0o755)
            for name in ("orca_grafana_mcp", "orca_model_calls"):
                shutil.copytree(
                    Path(__file__).resolve().parent.parent / name,
                    source / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for path in source.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            await environment.upload_dir(source, f"{WORK}/src")
        command = (
            f"python3 -m pip install --disable-pip-version-check --no-index "
            f"--find-links={WORK}/runtime/wheels --require-hashes -r {WORK}/runtime/requirements.lock "
            f"--target={WORK}/deps > {LOGS}/runtime-install.log 2>&1"
        )
        result = await environment.exec(command, timeout_sec=300)
        if result.return_code != 0:
            raise RuntimeError("offline native MCP runtime installation failed")

    async def run(self, instruction, environment, context):
        config = {
            **self.settings,
            "model": self.model_name,
            "options": self.options,
            "instruction": instruction,
            "session_id": self.session_id or str(self.context_id),
            "binary": f"{WORK}/runtime/mcp-grafana",
            "output_dir": LOGS,
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(config))
            path.chmod(0o644)
            await environment.upload_file(path, f"{WORK}/config.json")
        key = (
            "ANTHROPIC_API_KEY"
            if self.model_name.startswith("anthropic/")
            else "FIREWORKS_AI_API_KEY"
        )
        secret = self.extra_env.get(key) or os.environ.get(key)
        if key == "FIREWORKS_AI_API_KEY" and not secret:
            secret = self.extra_env.get("FIREWORKS_API_KEY") or os.environ.get(
                "FIREWORKS_API_KEY"
            )
        if not secret:
            raise ValueError(f"missing {key}")
        # Clear unrelated inherited credentials in the actual worker process. The
        # shell's BASH_ENV readiness gate exports the trial-local Grafana URL first.
        launcher = (
            "import os,sys; "
            f"names=['PATH','HOME','LANG','GRAFANA_URL','{key}']; "
            "env={k:os.environ[k] for k in names if k in os.environ}; "
            f"env['PYTHONPATH']='{WORK}/deps:{WORK}/src'; "
            "env['LITELLM_LOCAL_MODEL_COST_MAP']='True'; "
            f"os.execve(sys.executable,[sys.executable,'-m','orca_grafana_mcp.runner','{WORK}/config.json'],env)"
        )
        try:
            result = await environment.exec(
                f"python3 -c {shlex.quote(launcher)} > {LOGS}/runner.log 2>&1",
                env={key: secret},
            )
            if result.return_code != 0:
                raise RuntimeError(
                    "native MCP agent failed; inspect private agent logs"
                )
        finally:
            try:
                await environment.download_file(
                    f"{LOGS}/metrics.json", self.logs_dir / "metrics.json"
                )
            except Exception:
                pass  # Harbor's final log synchronization can backfill this.
            self.populate_context_post_run(context)

    def populate_context_post_run(self, context):
        path = self.logs_dir / "metrics.json"
        if path.is_file():
            metrics = json.loads(path.read_text())
            for field in (
                "n_input_tokens",
                "n_output_tokens",
                "n_cache_tokens",
                "cost_usd",
            ):
                setattr(context, field, metrics[field])
