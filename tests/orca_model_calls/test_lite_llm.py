"""Exercise the real pinned LiteLLM stack with a local HTTP provider."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orca_model_calls.capture import Recorder
from orca_model_calls.lite_llm import CapturingLiteLLM


@pytest.fixture
def provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            body = json.dumps(
                {
                    "id": f"response-{len(requests)}",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "synthetic answer",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests
    server.shutdown()
    thread.join()
    server.server_close()


def test_real_litellm_callback(provider):
    url, requests = provider

    async def run():
        recorders = [Recorder(), Recorder()]
        llms = [
            CapturingLiteLLM(
                "openai/gpt-4o-mini",
                recorder=r,
                api_base=url,
                api_key="synthetic-secret",
                callback_timeout=3,
            )
            for r in recorders
        ]
        await asyncio.gather(
            *(llm.call(f"synthetic prompt {i}") for i, llm in enumerate(llms))
        )
        for i, recorder in enumerate(recorders):
            extension = recorder.extension()
            assert extension["rejected_calls"] == 0, recorder.diagnostics
            assert len(extension["calls"]) == 1
            call = extension["calls"][0]
            assert call["messages"] == [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": f"synthetic prompt {i}"}],
                }
            ]
            assert call["runtime_provider"] == "openai"
            assert call["response"]["finish_reason"]["unified"] == "stop"
            assert "synthetic-secret" not in json.dumps(extension)
        assert len(requests) == 2

    asyncio.run(run())
