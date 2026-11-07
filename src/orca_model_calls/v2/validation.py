"""Strict v2 schema and cross-message association validation."""

from functools import lru_cache
from importlib.resources import files

import jsonschema

from orca_model_calls.atif import loads
from . import validate_history


@lru_cache(maxsize=1)
def validator():
    schema = loads(files("orca_model_calls").joinpath("schema-v2.json").read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def require_integers(value):
    if isinstance(value, dict):
        for child in value.values():
            require_integers(child)
    elif isinstance(value, list):
        for child in value:
            require_integers(child)
    elif type(value) is not int:
        raise ValueError("accounting numbers must use integer JSON notation")


def validate_call(call):
    # A small envelope makes the single source of schema truth reusable.
    schema = validator().schema
    jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/call", "$defs": schema["$defs"]},
        format_checker=jsonschema.FormatChecker(),
    ).validate(call)
    require_integers(
        [
            call["ordinal"],
            call["step_number"],
            call["request_options"]["max_tokens"],
            call["response"]["step_number"],
            call["response"]["usage"],
        ]
    )
    validate_history(call["messages"])
    validate_history([*call["messages"], call["response"]["message"]], complete=False)
    names = [t["function"]["name"] for t in call["tools"]]
    if len(names) != len(set(names)):
        raise ValueError("duplicate tool names")


def validate(artifact):
    validator().validate(artifact)
    ext = artifact["extra"]["llmspec_model_calls"]
    require_integers(
        [
            ext[key]
            for key in (
                "capture_byte_limit",
                "pending_requests",
                "uncertain_requests",
                "successful_responses",
                "dropped_calls",
                "rejected_calls",
            )
        ]
    )
    for call in ext["calls"]:
        validate_call(call)
    if (
        ext["successful_responses"]
        != len(ext["calls"]) + ext["dropped_calls"] + ext["rejected_calls"]
    ):
        raise ValueError("unaccounted successful responses")
    if ext["finalized"] and ext["pending_requests"]:
        raise ValueError("finalized capture has pending requests")
