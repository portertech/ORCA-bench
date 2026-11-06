"""Closed mapping of LiteLLM chat messages; no best-effort coercion."""

import json
from collections.abc import Mapping


class Unrepresentable(ValueError):
    """A successful call cannot be represented by this contract revision."""


def object_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    raise Unrepresentable("expected an object")


def provider_options(value):
    if "cache_control" not in value:
        return {}
    control = value["cache_control"]
    if not isinstance(control, dict) or not isinstance(control.get("type"), str):
        raise Unrepresentable("malformed cache_control")
    # Copy only the explicitly selected inert field, never callback metadata.
    return {
        "providerOptions": {
            "cache_control": json.loads(json.dumps(control, allow_nan=False))
        }
    }


def messages_to_wire(messages):
    if not isinstance(messages, list):
        raise Unrepresentable("messages must be a list")
    result = []
    for message in messages:
        msg = object_dict(message)
        if set(msg) - {
            "role",
            "content",
            "reasoning_content",
            "cache_control",
            "tool_calls",
            "function_call",
        }:
            raise Unrepresentable("unsupported message fields")
        role = msg.get("role")
        if role not in {"system", "user", "assistant"}:
            raise Unrepresentable("unsupported role")
        if msg.get("tool_calls") or msg.get("function_call"):
            raise Unrepresentable("native tool calls are not supported in v1 producer")
        content = msg.get("content")
        opts = provider_options(msg)
        if role == "system":
            if not isinstance(content, str) or msg.get("reasoning_content") is not None:
                raise Unrepresentable("system content must be a string")
            result.append({"role": role, "content": content, **opts})
            continue
        parts = []
        reasoning = msg.get("reasoning_content")
        if reasoning is not None:
            if role != "assistant" or not isinstance(reasoning, str):
                raise Unrepresentable("invalid reasoning_content")
            parts.append({"type": "reasoning", "text": reasoning})
        if isinstance(content, str):
            parts.append({"type": "text", "text": content})
        elif role == "user" and isinstance(content, list):
            for raw in content:
                part = object_dict(raw)
                if (
                    set(part) - {"type", "text", "cache_control"}
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                ):
                    raise Unrepresentable("unsupported or malformed content part")
                parts.append(
                    {"type": "text", "text": part["text"], **provider_options(part)}
                )
        else:
            raise Unrepresentable("unsupported content")
        result.append({"role": role, "content": parts, **opts})
    return result


def response_to_wire(response, completed_at):
    raw = object_dict(response)
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        raise Unrepresentable("missing response ID")
    if not isinstance(raw.get("model"), str) or not raw["model"]:
        raise Unrepresentable("missing runtime model")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise Unrepresentable("exactly one response choice is required")
    choice = object_dict(choices[0])
    msg = object_dict(choice.get("message"))
    # LiteLLM can add diagnostic fields; reject meaningful unmodeled payloads.
    for key in (
        "tool_calls",
        "function_call",
        "audio",
        "images",
        "refusal",
        "thinking_blocks",
        "provider_specific_fields",
    ):
        value = msg.get(key)
        if key == "provider_specific_fields" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if v is not None}
        if value:
            raise Unrepresentable("unsupported response payload")
    selected = {k: msg[k] for k in ("role", "content", "reasoning_content") if k in msg}
    output = messages_to_wire([selected])[0]
    if output["role"] != "assistant":
        raise Unrepresentable("response must be assistant")
    finish = choice.get("finish_reason")
    if not isinstance(finish, str) or not finish:
        raise Unrepresentable("missing finish reason")
    unified = {
        "stop": "stop",
        "length": "length",
        "content_filter": "content-filter",
        "tool_calls": "tool-calls",
        "function_call": "tool-calls",
    }.get(finish, "unknown")
    from datetime import datetime, timezone

    created = raw.get("created")
    if created is not None:
        if type(created) not in (int, float):
            raise Unrepresentable("invalid response timestamp")
        timestamp = datetime.fromtimestamp(created, timezone.utc).isoformat()
    else:
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        timestamp = completed_at.isoformat()
    usage_raw = object_dict(raw["usage"]) if raw.get("usage") is not None else {}
    usage = {"input_tokens": {}, "output_tokens": {}, "raw": {}}
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        count = usage_raw.get(source)
        if count is not None:
            if type(count) is not int or count < 0:
                raise Unrepresentable("invalid token count")
            usage[target]["total"] = count
    # Explicitly select numeric token metadata only; do not recurse through kwargs.
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
    ):
        value = usage_raw.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if type(v) is int and v >= 0}
        elif type(value) is not int or value < 0:
            raise Unrepresentable("invalid usage metadata")
        usage["raw"][key] = value
    return {
        "id": raw["id"],
        "timestamp": timestamp,
        "model": raw["model"],
        "message": output,
        "finish_reason": {"raw": finish, "unified": unified},
        "usage": usage,
    }
