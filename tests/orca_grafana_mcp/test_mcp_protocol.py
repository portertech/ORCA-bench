"""Opt-in real Go MCP server + official SDK against a synthetic Grafana HTTP API."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCA_GRAFANA_MCP_BINARY"),
    reason="set pinned MCP binary for protocol integration",
)


def test_real_stdio_discovery_and_grafana_proxy(tmp_path):
    from orca_grafana_mcp.mcp_client import connect
    from orca_grafana_mcp.tools import ToolRouter

    paths = []
    sources = [
        {
            "id": 1,
            "uid": "webstore-metrics",
            "name": "Synthetic metrics",
            "type": "prometheus",
            "url": "http://prometheus:9090",
            "access": "proxy",
        },
        {
            "id": 2,
            "uid": "webstore-logs",
            "name": "Synthetic logs",
            "type": "grafana-opensearch-datasource",
            "url": "http://opensearch:9200",
            "access": "proxy",
        },
        {
            "id": 3,
            "uid": "webstore-traces",
            "name": "Synthetic traces",
            "type": "jaeger",
            "url": "http://jaeger:16686/jaeger/ui",
            "access": "proxy",
        },
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if urlsplit(self.path).path == "/grafana/api/ds/query":
                paths.append(self.path)
                query = json.loads(body)["queries"][0]
                result = {
                    "results": {
                        query["refId"]: {
                            "frames": [
                                {
                                    "schema": {
                                        "refId": query["refId"],
                                        "fields": [
                                            {
                                                "name": "Time",
                                                "type": "time",
                                                "typeInfo": {"frame": "time.Time"},
                                            },
                                            {
                                                "name": "Value",
                                                "type": "number",
                                                "typeInfo": {"frame": "float64"},
                                                "labels": {"__name__": "up"},
                                            },
                                        ],
                                    },
                                    "data": {"values": [[1767225600000], [1]]},
                                }
                            ]
                        }
                    }
                }
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            paths.append(self.path)
            if path == "/grafana/api/health":
                result = {"database": "ok", "version": "12.3.1"}
            elif path == "/grafana/api/datasources":
                result = sources
            elif path.startswith("/grafana/api/datasources/uid/"):
                result = next(s for s in sources if s["uid"] == path.rsplit("/", 1)[-1])
            elif path.endswith("/api/services"):
                result = {"data": ["synthetic-checkout"], "errors": None}
            elif path.endswith("/api/v1/query"):
                result = {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {"metric": {"__name__": "up"}, "value": [1767225600, "1"]}
                        ],
                    },
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise():
        async with connect(
            os.environ["ORCA_GRAFANA_MCP_BINARY"],
            f"http://127.0.0.1:{server.server_port}/grafana",
            tmp_path / "server.log",
            test_endpoint=True,
        ) as client:
            catalog = await client.discover()
            router = ToolRouter(client, catalog, source_root=tmp_path)
            assert "grafana_api_request" not in router.schemas
            sources_result = await router.dispatch(
                "grafana_list_datasources", "{}", "call-1"
            )
            assert "webstore-logs" in sources_result
            trace_result = await router.dispatch("jaeger_services", "{}", "call-2")
            assert "synthetic-checkout" in trace_result
            metric_result = await router.dispatch(
                "grafana_query_prometheus",
                json.dumps(
                    {
                        "datasourceUid": "webstore-metrics",
                        "expr": "up",
                        "queryType": "instant",
                        "endTime": "2026-01-01T00:00:00Z",
                    }
                ),
                "call-3",
            )
            assert "__name__" in metric_result and "up" in metric_result, paths
            assert len(client.invocations) == 3 and not any(
                c["is_error"] for c in client.invocations
            )

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert all(urlsplit(path).path.startswith("/grafana/") for path in paths)
