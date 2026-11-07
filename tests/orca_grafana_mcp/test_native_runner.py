"""Real LiteLLM HTTP translation + callbacks; no paid APIs or MCP dependency."""

import asyncio
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import litellm
import pytest

from orca_grafana_mcp.runner import run
from orca_model_calls.atif import loads, validate
from test_native_tools import Client


@pytest.mark.parametrize("provider", ["openai", "anthropic", "fireworks_ai"])
def test_real_native_two_turn_boundary(tmp_path, monkeypatch, provider):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            tool = len(requests) == 1
            if provider == "anthropic":
                content = (
                    [
                        {
                            "type": "thinking",
                            "thinking": "Synthetic analysis",
                            "signature": "synthetic-signature",
                        },
                        {
                            "type": "tool_use",
                            "id": "call_synthetic",
                            "name": "grafana_list_datasources",
                            "input": {},
                        },
                    ]
                    if tool
                    else [
                        {
                            "type": "text",
                            "text": "# Synthetic RCA\nObserved synthetic evidence.",
                        }
                    ]
                )
                response = {
                    "id": f"msg_synthetic_{len(requests)}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5-5",
                    "content": content,
                    "stop_reason": "tool_use" if tool else "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 5},
                }
            else:
                message = {
                    "role": "assistant",
                    "content": (
                        None
                        if tool
                        else "# Synthetic RCA\nObserved synthetic evidence."
                    ),
                }
                if tool:
                    message["tool_calls"] = [
                        {
                            "id": "call_synthetic",
                            "type": "function",
                            "function": {
                                "name": "grafana_list_datasources",
                                "arguments": "{}",
                            },
                        }
                    ]
                response = {
                    "id": f"synthetic_{len(requests)}",
                    "model": "synthetic-model",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls" if tool else "stop",
                            "message": message,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "total_tokens": 17,
                    },
                }
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "orca_grafana_mcp.runner.importlib.metadata.version", lambda name: "synthetic"
    )
    client = Client()
    config = {
        "output_dir": str(tmp_path / "logs"),
        "report_path": str(tmp_path / "report.md"),
        "source_root": str(tmp_path),
        "instruction": "Synthetic task",
        "session_id": "synthetic-session",
        "max_turns": 3,
        "model": f"{provider}/"
        + (
            "claude-sonnet-5-5"
            if provider == "anthropic"
            else (
                "accounts/fireworks/models/qwen3p8-max"
                if provider == "fireworks_ai"
                else "synthetic-model"
            )
        ),
        "options": {"max_tokens": 1024, "tool_choice": "auto"},
    }
    if provider == "fireworks_ai":
        config["options"].update(
            reasoning_effort="medium",
            allowed_openai_params=["tools", "tool_choice", "reasoning_effort"],
        )
    transport = partial(
        litellm.acompletion,
        api_base=f"http://127.0.0.1:{server.server_port}",
        api_key="synthetic-key",
    )
    try:
        recorder = asyncio.run(run(config, client=client, completion=transport))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(requests) == 2
    assert requests[0].get("tools")
    assert requests[0].get("tool_choice") == (
        {"type": "auto"} if provider == "anthropic" else "auto"
    )
    if provider == "fireworks_ai":
        assert requests[0].get("reasoning_effort") == "medium"
    assert client.invocations[0]["mcp_tool"] == "list_datasources"
    assert (tmp_path / "report.md").read_text().startswith("# Synthetic RCA")
    artifact = validate(
        loads((tmp_path / "logs/llmspec-trajectory.atif.json").read_bytes())
    )
    ext = artifact["extra"]["llmspec_model_calls"]
    assert (
        len(ext["calls"]) == 2 and ext["successful_responses"] == 2 and ext["finalized"]
    )
    assert not ext["uncertain_requests"] and not ext["rejected_calls"]
    assert ext["calls"][1]["messages"][-1]["tool_call_id"] == "call_synthetic"
    assert (
        json.loads((tmp_path / "logs/metrics.json").read_text())["n_input_tokens"] == 24
    )
    try:
        from harbor.models.trajectories.trajectory import Trajectory
    except ImportError:
        Trajectory = None  # Isolated worker intentionally has no Harbor dependency.
    if Trajectory is not None:
        normal = Trajectory.model_validate_json(
            (tmp_path / "logs/trajectory.json").read_bytes()
        )
        assert normal.final_metrics.total_prompt_tokens == 24
        assert normal.steps[1].observation.results[0].source_call_id == "call_synthetic"
    if provider == "anthropic":
        # The real translator must carry the signed thinking block into turn two.
        assert "synthetic-signature" in json.dumps(requests[1])


def test_provider_error_is_uncertain_and_checkpointed(tmp_path):
    from orca_grafana_mcp.llm import complete
    from orca_model_calls.v2 import NativeRecorder

    recorder = NativeRecorder()
    checkpoints = []

    async def failure(**kwargs):
        raise RuntimeError("synthetic provider failure")

    with pytest.raises(RuntimeError):
        asyncio.run(
            complete(
                recorder,
                "openai/synthetic",
                [],
                [],
                {},
                lambda: checkpoints.append(recorder.extension()),
                completion=failure,
            )
        )
    assert len(checkpoints) == 2 and checkpoints[0]["pending_requests"] == 1
    assert recorder.uncertain_requests == 1 and recorder.pending_requests == 0
