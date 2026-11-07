"""Closed native-tool routing: task Grafana reads and bounded source inspection."""

import hashlib
import json
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode

import jsonschema

from orca_model_calls.atif import loads
from orca_model_calls.capture import encode

ALLOWED = {
    "list_datasources",
    "get_datasource",
    "search_dashboards",
    "get_dashboard_by_uid",
    "get_dashboard_summary",
    "get_dashboard_property",
    "get_dashboard_panel_queries",
    "list_prometheus_metric_names",
    "list_prometheus_metric_metadata",
    "list_prometheus_label_names",
    "list_prometheus_label_values",
    "query_prometheus",
    "query_elasticsearch",
}
REQUIRED = {
    "list_datasources",
    "query_prometheus",
    "query_elasticsearch",
    "grafana_api_request",
}
DATASOURCES = {"webstore-metrics", "webstore-logs", "webstore-traces"}
SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".mod",
    ".sum",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".h",
    ".c",
    ".rs",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".txt",
    ".properties",
    ".proto",
    ".sql",
    ".sh",
    ".xml",
    ".gradle",
}
BLOCKED = {
    "tests",
    "test",
    "solutions",
    "solution",
    "verifier",
    ".git",
    "__pycache__",
    "node_modules",
    "credentials",
    ".env",
    "secrets",
    "vendor",
}


def definition(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


def absolute_time(value):
    if not isinstance(value, str):
        raise ValueError("absolute RFC3339 timestamp required")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return date


class ToolRouter:
    def __init__(
        self,
        client,
        catalog,
        *,
        source_root="/app/opentelemetry-demo/src",
        output_limit=32000,
    ):
        self.client = client
        self.source_root = Path(source_root).resolve()
        self.output_limit = output_limit
        self.routing = {}
        self.definitions = []
        names = {t.name for t in catalog}
        if not REQUIRED <= names:
            raise ValueError("required MCP tools are missing")
        for tool in sorted(catalog, key=lambda t: t.name):
            if tool.name in ALLOWED:
                name = "grafana_" + tool.name
                schema = tool.input_schema
                self.definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool.description
                            or "Read task-local Grafana",
                            "parameters": schema,
                        },
                    }
                )
                self.routing[name] = tool.name
        text = {"type": "string"}
        number = {"type": "integer", "minimum": 1, "maximum": 100}
        self.definitions.extend(
            [
                definition(
                    "jaeger_services",
                    "List Jaeger services through Grafana's datasource proxy.",
                    {},
                ),
                definition(
                    "jaeger_search",
                    "Search historical Jaeger traces. Times are absolute RFC3339 with timezone.",
                    {
                        "service": text,
                        "start": text,
                        "end": text,
                        "limit": number,
                        "tags": text,
                        "min_duration": text,
                    },
                    ["service", "start", "end"],
                ),
                definition(
                    "jaeger_trace",
                    "Get one Jaeger trace through Grafana.",
                    {"trace_id": {"type": "string", "pattern": "^[a-fA-F0-9]{16,32}$"}},
                    ["trace_id"],
                ),
                definition(
                    "source_list",
                    "List bounded source files. Paths relative to the service source root.",
                    {"path": text},
                ),
                definition(
                    "source_read",
                    "Read source text, with explicit line pagination. Paths relative to source root.",
                    {
                        "path": text,
                        "offset": {"type": "integer", "minimum": 1},
                        "lines": {"type": "integer", "minimum": 1, "maximum": 300},
                    },
                    ["path"],
                ),
                definition(
                    "source_search",
                    "Literal case-sensitive search of bounded service source files.",
                    {
                        "path": text,
                        "text": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    ["text"],
                ),
            ]
        )
        self.schemas = {
            t["function"]["name"]: t["function"]["parameters"] for t in self.definitions
        }
        if len(self.schemas) != len(self.definitions):
            raise ValueError("duplicate native tool names")
        self.fingerprint = hashlib.sha256(encode(self.definitions)).hexdigest()

    def safe_path(self, value):
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("source path denied")
        path = (self.source_root / relative).resolve(strict=True)
        local = path.relative_to(self.source_root)
        if any(p.lower() in BLOCKED or p.startswith(".") for p in local.parts):
            raise ValueError("source path denied")
        if path.is_file() and path.suffix not in SOURCE_EXTENSIONS:
            raise ValueError("source type denied")
        return path

    def source(self, name, args):
        path = self.safe_path(args.get("path", "."))
        if name == "source_read":
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("source file absent/too large")
            lines = path.read_text().splitlines()
            offset = args.get("offset", 1) - 1
            return {
                "lines": [
                    {"line": i + 1, "text": line}
                    for i, line in enumerate(
                        lines[offset : offset + args.get("lines", 100)], offset
                    )
                ],
                "total_lines": len(lines),
            }
        files, hits, examined = [], [], 0
        iterator = path.rglob("*") if path.is_dir() else [path]
        for candidate in iterator:
            try:
                candidate = self.safe_path(str(candidate.relative_to(self.source_root)))
                if not candidate.is_file():
                    continue
            except (ValueError, OSError):
                continue
            examined += 1
            if examined > 3000:
                break
            relative = str(candidate.relative_to(self.source_root))
            if name == "source_list":
                files.append(relative)
                if len(files) >= 200:
                    break
            elif candidate.stat().st_size <= 256 * 1024:
                for index, line in enumerate(
                    candidate.read_text(errors="replace").splitlines(), 1
                ):
                    if args["text"] in line:
                        hits.append(
                            {"path": relative, "line": index, "text": line[:1000]}
                        )
                        if len(hits) >= 100:
                            break
                if len(hits) >= 100:
                    break
        return {
            "files": files,
            "hits": hits,
            "bounded": True,
            "files_examined": examined,
        }

    async def dispatch(self, name, arguments, call_id):
        try:
            args = loads(arguments)
            if name not in self.schemas:
                raise ValueError("unknown tool")
            jsonschema.validate(args, self.schemas[name])
            if name.startswith("source_"):
                result = self.source(name, args)
            elif name.startswith("jaeger_"):
                prefix = "/api/datasources/proxy/uid/webstore-traces/api/"
                if name == "jaeger_services":
                    endpoint = prefix + "services"
                elif name == "jaeger_trace":
                    endpoint = prefix + "traces/" + args["trace_id"]
                else:
                    start, end = absolute_time(args["start"]), absolute_time(
                        args["end"]
                    )
                    if end <= start or (end - start).total_seconds() > 86400:
                        raise ValueError("invalid trace query window")
                    query = {
                        "service": args["service"],
                        "start": int(start.timestamp() * 1_000_000),
                        "end": int(end.timestamp() * 1_000_000),
                        "limit": args.get("limit", 20),
                    }
                    if "tags" in args:
                        query["tags"] = args["tags"]
                    if "min_duration" in args:
                        query["minDuration"] = args["min_duration"]
                    endpoint = prefix + "traces?" + urlencode(query)
                result = await self.client.call(
                    "grafana_api_request",
                    {"method": "GET", "endpoint": endpoint},
                    call_id,
                )
            else:
                self.validate_query(name, args)
                result = await self.client.call(self.routing[name], args, call_id)
            text = encode(result).decode()
            if len(text) > self.output_limit:
                text = json.dumps(
                    {
                        "bounded": True,
                        "original_characters": len(text),
                        "prefix": text[: self.output_limit],
                        "notice": "Result clipped; narrow query or request fewer fields/documents.",
                    }
                )
            return text
        except (ValueError, KeyError, OSError, jsonschema.ValidationError):
            # No exception text: it can contain paths, query contents, or credentials.
            return '{"error":"Tool input/result rejected; use the documented schema, source root, datasource and absolute times."}'
        except TimeoutError:
            return '{"error":"Tool request timed out; narrow the query."}'

    @staticmethod
    def validate_query(name, args):
        uid = args.get("datasourceUid") or args.get("uid")
        if (
            uid is not None
            and uid not in DATASOURCES
            and name == "grafana_get_datasource"
        ):
            raise ValueError("unknown datasource")
        if "datasourceUid" in args:
            expected = (
                "webstore-logs"
                if name == "grafana_query_elasticsearch"
                else "webstore-metrics"
            )
            if args["datasourceUid"] != expected:
                raise ValueError("wrong datasource")
        if any(args.get(k) for k in ("projectName", "scoping", "headers")):
            raise ValueError("external query options denied")
        if name in {"grafana_query_prometheus", "grafana_query_elasticsearch"}:
            end = absolute_time(args["endTime"])
            needs_start = (
                name == "grafana_query_elasticsearch"
                or args.get("queryType", "instant") == "range"
            )
            if needs_start or "startTime" in args:
                start = absolute_time(args["startTime"])
                duration = (end - start).total_seconds()
                if not 0 <= duration <= 86400:
                    raise ValueError("invalid time window")
                if args.get("queryType") == "range":
                    step = args.get("stepSeconds", 0)
                    if step <= 0 or duration / step > 11000:
                        raise ValueError("invalid range step")
        for key in ("startRfc3339", "endRfc3339", "start", "end"):
            if key in args:
                absolute_time(args[key])
        for key in ("limit", "size", "limitPerMetric"):
            if key in args and not 1 <= args[key] <= 100:
                raise ValueError("query limit out of bounds")
        if (
            name == "grafana_query_elasticsearch"
            and args.get("index", "otel-logs-*") != "otel-logs-*"
        ):
            raise ValueError("wrong log index")
