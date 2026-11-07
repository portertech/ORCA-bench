"""Per-trial stdio session. No model credentials are passed to the child."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER_ARGS = ["--transport", "stdio", "--disable-write", "--enabled-tools",
               "search,datasource,dashboard,prometheus,elasticsearch,api", "--usage-stats=disabled"]
TASK_GRAFANA_URL = "http://frontend-proxy:8080/grafana"


@asynccontextmanager
async def connect(binary, grafana_url, log_path, *, token=None, test_endpoint=False):
    if not test_endpoint and grafana_url.rstrip("/") != TASK_GRAFANA_URL:
        raise ValueError("only the trial-local Grafana endpoint is supported")
    parts = urlsplit(grafana_url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("invalid Grafana endpoint")
    env = {"PATH": os.defpath, "GRAFANA_URL": grafana_url}
    if token:
        env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = token
    params = StdioServerParameters(command=str(binary), args=SERVER_ARGS, env=env)
    with open(log_path, "w", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=60) as session:
                await session.initialize()
                yield GrafanaSession(session)


class GrafanaSession:
    def __init__(self, session):
        self.session = session
        self.invocations = []

    async def discover(self):
        from mcp import types
        catalog, cursors, cursor = [], set(), None
        while True:
            page = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            catalog.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in cursors or len(catalog) > 1000:
                raise ValueError("invalid discovery pagination")
            cursors.add(cursor)
        names = [tool.name for tool in catalog]
        if len(names) != len(set(names)):
            raise ValueError("duplicate MCP tool names")
        return catalog

    async def call(self, name, arguments, call_id):
        # The dispatcher, not the model, selects actual MCP names/routes.
        result = await self.session.call_tool(name, arguments, read_timeout_seconds=60)
        raw = result.model_dump(mode="json", exclude_none=True, by_alias=True)
        self.invocations.append({"tool_call_id": call_id, "mcp_tool": name,
                                 "arguments": arguments, "is_error": bool(raw.get("isError"))})
        if any(part.get("type") != "text" for part in raw.get("content", [])):
            raise ValueError("unsupported nontext MCP result")
        return raw
