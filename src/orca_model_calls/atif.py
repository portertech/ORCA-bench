"""Strict ATIF envelope, atomic writes, and public semantic validation."""

import copy
import json
import os
import re
import tempfile
from datetime import datetime
import uuid
from importlib.resources import files
from pathlib import Path

import jsonschema

from . import PRODUCER_VERSION
from .capture import encode


def loads(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    def nonfinite(value):
        raise ValueError("non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)


def envelope(recorder, session_id, trajectory=None, trajectory_id=None):
    trajectory = trajectory or {}
    steps = copy.deepcopy(trajectory.get("steps", []))
    for index, step in enumerate(steps, 1):
        source = step.get("source")
        if source == "system":
            step["source"] = "environment"
            step.setdefault("extra", {})["original_source"] = source
        elif source not in {"user", "agent", "environment"}:
            raise ValueError("unsupported transcript source")
        step["step_id"] = index
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "trajectory_id": trajectory_id or str(uuid.uuid4()),
        "agent": {"name": "terminus-2", "version": PRODUCER_VERSION},
        "steps": steps,
        "final_metrics": trajectory.get("final_metrics") or {},
        "extra": {"llmspec_model_calls": recorder.extension()},
    }


def validate(artifact):
    # The packaged copy and repository schema are the same file (symlink in source).
    schema = loads(files("orca_model_calls").joinpath("schema.json").read_text())
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(artifact)
    encode(artifact)  # Reject non-finite values even inside inert metadata.
    ext = artifact["extra"]["llmspec_model_calls"]

    def strict_integers(value):
        if isinstance(value, float):
            raise ValueError("extension numbers must use integer JSON notation")
        if isinstance(value, dict):
            for child in value.values():
                strict_integers(child)
        elif isinstance(value, list):
            for child in value:
                strict_integers(child)

    strict_integers(ext)
    ids, response_ids, ordinals = set(), set(), []
    for call in ext["calls"]:
        ordinal = call["ordinal"]
        response = call["response"]
        timestamp = response["timestamp"]
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            timestamp,
        ):
            raise ValueError("invalid response timestamp")
        datetime.fromisoformat(timestamp)
        if call["request_id"] in ids or response["id"] in response_ids:
            raise ValueError("duplicate call identity")
        ids.add(call["request_id"])
        response_ids.add(response["id"])
        ordinals.append(ordinal)
        if (
            call["execution_generation_id"] != ext["execution_generation_id"]
            or call["step_number"] != ordinal
        ):
            raise ValueError("mismatched execution or step identity")
        if (
            response["request_id"] != call["request_id"]
            or response["step_number"] != ordinal
            or response["model"] != call["runtime_model"]
        ):
            raise ValueError("mismatched response association")
    if ordinals != sorted(set(ordinals)):
        raise ValueError("ordinals must be unique and strictly increasing")
    loss = ext["dropped_calls"] + ext["rejected_calls"]
    if ordinals and ordinals[-1] > len(ordinals) + loss:
        raise ValueError("unaccounted ordinal gap")
    if not loss and ordinals != list(range(1, len(ordinals) + 1)):
        raise ValueError("lossless ordinals must be consecutive")
    if ext["dropped_calls"] and not ext["truncated"]:
        raise ValueError("dropped calls require truncated=true")
    if len(encode(ext)) > ext["capture_byte_limit"] and not ext["truncated"]:
        raise ValueError("extension exceeds its declared bound")
    step_ids = [step["step_id"] for step in artifact["steps"]]
    if any(type(step_id) is not int for step_id in step_ids):
        raise ValueError("transcript IDs must use integer JSON notation")
    if step_ids != sorted(set(step_ids)):
        raise ValueError("transcript IDs must be unique and increasing")
    return artifact


def atomic_write(path, artifact):
    validate(artifact)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encode(artifact))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
