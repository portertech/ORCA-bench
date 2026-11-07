import asyncio
import json
from types import SimpleNamespace

import pytest

from orca_grafana_mcp.tools import ToolRouter, REQUIRED


class Client:
    def __init__(self):
        self.invocations = []

    async def discover(self):
        return [
            SimpleNamespace(
                name=name, description="Synthetic tool", input_schema={"type": "object"}
            )
            for name in REQUIRED
        ]

    async def call(self, name, arguments, call_id):
        self.invocations.append(
            {"mcp_tool": name, "arguments": arguments, "tool_call_id": call_id}
        )
        text = "Synthetic result"
        if name == "list_datasources":
            text = json.dumps(
                {
                    "datasources": [
                        {"uid": "webstore-metrics", "type": "prometheus"},
                        {
                            "uid": "webstore-logs",
                            "type": "grafana-opensearch-datasource",
                        },
                        {"uid": "webstore-traces", "type": "jaeger"},
                    ]
                }
            )
        return {"content": [{"type": "text", "text": text}], "isError": False}


def router(tmp_path):
    client = Client()
    return (
        ToolRouter(client, asyncio.run(client.discover()), source_root=tmp_path),
        client,
    )


@pytest.mark.parametrize(
    "path",
    ["../report.md", "/proc/self/environ", ".env", "tests/answer.py", "escape.py"],
)
def test_source_confinement(tmp_path, path):
    (tmp_path / ".env").write_text("synthetic-secret")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/answer.py").write_text("not task source")
    (tmp_path / "escape.py").symlink_to(tmp_path.parent / "outside.py")
    (tmp_path.parent / "outside.py").write_text("outside")
    r, _ = router(tmp_path)
    result = json.loads(
        asyncio.run(r.dispatch("source_read", json.dumps({"path": path}), "id"))
    )
    assert "error" in result


def test_bounded_source_read_search(tmp_path):
    (tmp_path / "service.py").write_text("one\nneedle\nthree\n")
    r, _ = router(tmp_path)
    result = json.loads(
        asyncio.run(
            r.dispatch(
                "source_read", '{"path":"service.py","offset":2,"lines":1}', "id"
            )
        )
    )
    assert result["lines"] == [{"line": 2, "text": "needle"}]
    assert (
        json.loads(asyncio.run(r.dispatch("source_search", '{"text":"needle"}', "id")))[
            "hits"
        ][0]["line"]
        == 2
    )


def test_typed_jaeger_builds_fixed_route(tmp_path):
    r, c = router(tmp_path)
    args = {
        "service": "checkout/../../admin",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:05:00Z",
    }
    asyncio.run(r.dispatch("jaeger_search", json.dumps(args), "call"))
    invocation = c.invocations[0]
    assert invocation["mcp_tool"] == "grafana_api_request"
    assert invocation["arguments"]["method"] == "GET"
    assert invocation["arguments"]["endpoint"].startswith(
        "/api/datasources/proxy/uid/webstore-traces/api/traces?service=checkout%2F"
    )
    assert "start=1767225600000000" in invocation["arguments"]["endpoint"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("grafana_api_request", {"endpoint": "/api/admin/settings"}),
        ("jaeger_services", {"headers": {"Authorization": "synthetic"}}),
        ("jaeger_trace", {"trace_id": "../services"}),
        ("jaeger_search", {"service": "x", "start": "now-1h", "end": "now"}),
        (
            "grafana_query_prometheus",
            {"datasourceUid": "other", "expr": "up", "endTime": "2026-01-01T00:00:00Z"},
        ),
        (
            "grafana_query_elasticsearch",
            {
                "datasourceUid": "webstore-logs",
                "index": "otel-logs-*",
                "query": "*",
                "startTime": "now-1h",
                "endTime": "now",
            },
        ),
    ],
)
def test_reject_unsafe_queries_before_mcp(tmp_path, name, args):
    r, c = router(tmp_path)
    result = json.loads(asyncio.run(r.dispatch(name, json.dumps(args), "call")))
    assert "error" in result and not c.invocations


def test_result_bounding_is_explicit(tmp_path):
    r, c = router(tmp_path)
    r.output_limit = 20
    result = json.loads(asyncio.run(r.dispatch("jaeger_services", "{}", "id")))
    assert (
        result["bounded"]
        and result["original_characters"] > 20
        and len(result["prefix"]) == 20
    )
