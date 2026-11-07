"""In-container native function-call agent; invoked by the Harbor wrapper."""

import argparse
import asyncio
import importlib.metadata
import hashlib
from importlib.resources import files
import json
import logging
import os
from pathlib import Path
import signal
import sys
from datetime import datetime, timezone

from orca_model_calls.atif import atomic_write, envelope
from orca_model_calls.capture import encode
from orca_model_calls.v2 import NativeRecorder
from . import NAME, VERSION
from .llm import complete
from .tools import ToolRouter
from .trajectory import project


def write_json(path, value):
    # All private artifacts live in an agent-writable directory.
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def usage(recorder):
    if recorder.uncertain_requests:
        return {key: None for key in recorder.usage_totals}
    return dict(recorder.usage_totals)


async def run(config, *, client=None, completion=None):
    root = Path(config["output_dir"])
    if (root / "llmspec-trajectory.atif.json").exists() or (
        root / "trajectory.json"
    ).exists():
        raise FileExistsError("native capture requires a fresh execution directory")
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive, persistent marker also protects a kill before the first checkpoint.
    fd = os.open(
        root / ".native-execution.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(fd)
    recorder = NativeRecorder(
        byte_limit=config.get("capture_byte_limit", 67108864),
        call_byte_limit=config.get("capture_call_byte_limit", 4194304),
    )
    trajectory_id = recorder.execution_generation_id
    steps = [{"step_id": 1, "source": "user", "message": config["instruction"]}]
    router = None
    success = False

    def checkpoint():
        data = envelope(
            recorder,
            config["session_id"],
            {"steps": steps, "final_metrics": usage(recorder)},
            trajectory_id,
        )
        data["agent"] = {"name": NAME, "version": VERSION}
        atomic_write(root / "llmspec-trajectory.atif.json", data)
        normal = project(data, config["model"], router.definitions if router else [])
        write_json(root / "trajectory.json", normal)
        write_json(root / "metrics.json", usage(recorder))
        if client is not None:
            write_json(root / "mcp-invocations.json", client.invocations)

    async def loop(active_client):
        nonlocal client, router, success
        client = active_client
        catalog = await client.discover()
        router = ToolRouter(
            client,
            catalog,
            source_root=config.get("source_root", "/app/opentelemetry-demo/src"),
        )
        inventory = await client.call(
            "list_datasources", {"limit": 100}, "bootstrap-datasources"
        )
        if inventory.get("isError"):
            raise ValueError("datasource discovery failed")
        discovered = inventory.get("structuredContent")
        if discovered is None:
            discovered = json.loads(
                "".join(p["text"] for p in inventory.get("content", []))
            )
        actual = {d["uid"]: d["type"] for d in discovered["datasources"]}
        expected = {
            "webstore-metrics": "prometheus",
            "webstore-logs": "grafana-opensearch-datasource",
            "webstore-traces": "jaeger",
        }
        if any(actual.get(uid) != kind for uid, kind in expected.items()):
            raise ValueError("task datasource identity/type mismatch")
        source_hash = hashlib.sha256()
        for package in (Path(__file__).parent, Path(str(files("orca_model_calls")))):
            paths = [
                *package.rglob("*.py"),
                *package.glob("*.json"),
                *package.glob("prompts/*.txt"),
            ]
            for path in sorted(paths):
                source_hash.update(
                    (package.name + "/" + str(path.relative_to(package))).encode()
                )
                source_hash.update(path.read_bytes())
        provenance = {
            "agent": NAME,
            "version": VERSION,
            "mcp_grafana": "2.2.0",
            "transport": "stdio",
            "policy": "query-only-v1",
            "grafana_auth": "task-anonymous-admin-read-only-tools",
            "requested_model": config["model"],
            "options": config["options"],
            "tool_fingerprint": router.fingerprint,
            "source_sha256": source_hash.hexdigest(),
            "model_catalog": "bundled; explicit allowlist for configured reasoning controls",
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("litellm", "mcp", "jsonschema")
            },
        }
        write_json(root / "runtime.json", provenance)
        messages = [
            {
                "role": "system",
                "content": files("orca_grafana_mcp")
                .joinpath("prompts/system.txt")
                .read_text(),
            },
            {"role": "user", "content": config["instruction"]},
            {
                "role": "user",
                "content": "Runtime verified task-local Grafana datasource types: "
                + json.dumps(expected),
            },
        ]
        for turn in range(config.get("max_turns", 100)):
            message = await complete(
                recorder,
                config["model"],
                messages,
                router.definitions,
                config["options"],
                checkpoint,
                completion=completion,
            )
            messages.append(message)
            calls = message.get("tool_calls", [])
            step = {
                "step_id": len(steps) + 1,
                "source": "agent",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": message.get("content") or "",
                "extra": {"native_message": message},
            }
            steps.append(step)
            checkpoint()
            if not calls:
                if not message.get("content", "") or not message["content"].strip():
                    raise ValueError("empty final answer")
                if not any(
                    not call["tool_call_id"].startswith("bootstrap-")
                    and not call.get("is_error")
                    for call in client.invocations
                ):
                    raise ValueError(
                        "final answer did not successfully use MCP from a model tool call"
                    )
                if (
                    recorder.rejected_calls
                    or recorder.dropped_calls
                    or recorder.uncertain_requests
                ):
                    raise ValueError("incomplete model-call capture")
                report = root / "final-report.md"
                temporary = report.with_suffix(".tmp")
                temporary.write_text(message["content"])
                temporary.replace(report)
                # /app is root-owned; only the official pre-created file is writable.
                with open(config.get("report_path", "/app/report.md"), "w") as target:
                    target.write(message["content"])
                    target.flush()
                    os.fsync(target.fileno())
                success = True
                return
            if len(calls) > 16 or len(client.invocations) > 500:
                raise ValueError("tool execution budget exceeded")
            for call in calls:
                result = await router.dispatch(
                    call["function"]["name"], call["function"]["arguments"], call["id"]
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )
                steps.append(
                    {
                        "step_id": len(steps) + 1,
                        "source": "environment",
                        "message": result,
                        "extra": {"tool_call_id": call["id"]},
                    }
                )
                checkpoint()
        raise ValueError("agent turn limit exceeded")

    checkpoint()
    try:
        if client is None:
            from .mcp_client import connect

            async with connect(
                config["binary"],
                os.environ.get("GRAFANA_URL", ""),
                root / "mcp-server.log",
            ) as active:
                await loop(active)
        else:
            await loop(client)
    except BaseException as error:
        success = False

        def diagnosis(error):
            result = {"type": type(error).__name__}
            status = getattr(error, "status_code", None)
            if type(status) is int:
                result["http_status"] = status
            if isinstance(error, BaseExceptionGroup):
                result["children"] = [diagnosis(child) for child in error.exceptions]
            return result

        # Types/status only: provider exception text can contain prompts or keys.
        write_json(root / "failure.json", diagnosis(error))
        raise
    finally:
        recorder.finalized = recorder.pending_requests == 0
        checkpoint()
        write_json(
            root / "execution.json",
            {"success": success, "finalized": recorder.finalized},
        )
    return recorder


def main():
    os.umask(0o077)
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    args = parser.parse_args()

    async def entry():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
        await run(json.loads(Path(args.config).read_text()))

    try:
        asyncio.run(entry())
    except BaseException as error:
        print(
            f"Agent failed ({type(error).__name__}); inspect private diagnostics.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
