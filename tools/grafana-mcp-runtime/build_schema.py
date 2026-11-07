"""Generate the self-contained v2 schema without changing v1's semantics."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
schema = json.loads((ROOT / "schemas/llmspec-model-calls-v1.schema.json").read_text())
schema["$id"] = "urn:orca-bench:llmspec-model-calls-v2"
defs = schema["$defs"]


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


string = {"type": "string", "minLength": 1}
function = obj({"name": string, "arguments": {"type": "string"}})
defs["toolCall"] = obj({"id": string, "type": {"const": "function"}, "function": function})
thinking = {"oneOf": [
    obj({"type": {"const": "thinking"}, "thinking": {"type": "string"}, "signature": {"type": "string"}}),
    obj({"type": {"const": "redacted_thinking"}, "data": {"type": "string"}}),
]}
assistant = obj({"role": {"const": "assistant"}, "content": {"type": ["string", "null"]},
                 "reasoning_content": {"type": "string"},
                 "thinking_blocks": {"type": "array", "items": thinking},
                 "tool_calls": {"type": "array", "items": {"$ref": "#/$defs/toolCall"}}},
                ["role", "content"])
defs["assistant"] = assistant
defs["message"] = {"oneOf": [
    obj({"role": {"enum": ["system", "user"]}, "content": {"type": "string"}}),
    assistant,
    obj({"role": {"const": "tool"}, "content": {"type": "string"}, "tool_call_id": string}),
]}
defs["tool"] = obj({"type": {"const": "function"}, "function": obj({
    "name": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"},
    "description": {"type": "string"},
    "parameters": {"type": "object"},
})})
call = defs["call"]
call["properties"]["tools"] = {"type": "array", "items": {"$ref": "#/$defs/tool"}}
call["properties"]["request_options"] = obj({
    "max_tokens": {"type": "integer", "minimum": 1},
    "temperature": {"type": "number", "minimum": 0, "maximum": 2},
    "reasoning_effort": {"enum": ["none", "low", "medium", "high", "xhigh"]},
    "thinking": obj({"type": {"enum": ["disabled", "between_tools"]}}),
    "tool_choice": {"const": "auto"},
}, ["max_tokens", "tool_choice"])
call["required"].append("request_options")
props = defs["extension"]["properties"]
props["schema_version"] = {"const": "llmspec.model-calls.v2"}
props["capture_point"] = {"const": "litellm_chat_boundary"}
props["finalized"] = {"type": "boolean"}
for field in ("pending_requests", "uncertain_requests", "successful_responses"):
    props[field] = {"type": "integer", "minimum": 0}
defs["extension"]["required"].extend(["finalized", "pending_requests", "uncertain_requests", "successful_responses"])
# The v1 part definitions are not used by v2's native LiteLLM messages.
for name in ("part", "textPart"):
    del defs[name]
data = json.dumps(schema, indent=2) + "\n"
(ROOT / "schemas/llmspec-model-calls-v2.schema.json").write_text(data)
(ROOT / "tools/agento11y-export/schema-v2.json").write_text(data)
