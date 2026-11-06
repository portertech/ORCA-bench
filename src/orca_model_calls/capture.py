"""Immutable call records, identities, and whole-call capture budgets."""

import json
import threading
import uuid
from dataclasses import dataclass

from . import SCHEMA_VERSION
from .messages import Unrepresentable, messages_to_wire, response_to_wire


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class RequestIdentity:
    request_id: str
    execution_generation_id: str
    ordinal: int


@dataclass(frozen=True)
class CallRecord:
    identity: RequestIdentity
    payload: bytes


class Recorder:
    def __init__(self, byte_limit=64 * 1024 * 1024, call_byte_limit=4 * 1024 * 1024):
        if (
            type(byte_limit) is not int
            or type(call_byte_limit) is not int
            or min(byte_limit, call_byte_limit) <= 0
        ):
            raise ValueError("capture budgets must be positive integers")
        self.byte_limit = byte_limit
        self.call_byte_limit = call_byte_limit
        self.execution_generation_id = str(uuid.uuid4())
        self._lock = threading.RLock()
        self._next = 0
        self._records = []
        self._settled = set()
        self._response_ids = set()
        self.truncated = False
        self.dropped_calls = 0
        self.rejected_calls = 0
        # Sanitized codes only: exception messages may contain prompts or secrets.
        self.diagnostics = []

    def begin(self):
        with self._lock:
            self._next += 1
            return RequestIdentity(
                str(uuid.uuid4()), self.execution_generation_id, self._next
            )

    def reject(self, identity, code="unrepresentable"):
        with self._lock:
            if identity.request_id not in self._settled:
                self._settled.add(identity.request_id)
                self.rejected_calls += 1
                self.diagnostics.append(code)

    def record(
        self,
        identity,
        *,
        requested_model,
        provider,
        reasoning_effort,
        messages,
        response,
        completed_at,
        tools=None,
    ):
        with self._lock:
            if identity.request_id in self._settled:
                return
            try:
                if (
                    not isinstance(provider, str)
                    or not provider
                    or not isinstance(requested_model, str)
                    or not requested_model
                ):
                    raise Unrepresentable("missing provenance")
                if reasoning_effort not in {
                    None,
                    "none",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                }:
                    raise Unrepresentable("unsupported reasoning effort")
                if tools is not None and tools != []:
                    raise Unrepresentable("native tools are not supported initially")
                result = response_to_wire(response, completed_at)
                if result["id"] in self._response_ids:
                    raise Unrepresentable("duplicate response ID")
                self._response_ids.add(result["id"])
                call = {
                    "request_id": identity.request_id,
                    "execution_generation_id": identity.execution_generation_id,
                    "ordinal": identity.ordinal,
                    "step_number": identity.ordinal,
                    "requested_model": requested_model,
                    "runtime_provider": provider,
                    "runtime_model": result["model"],
                    "messages": messages_to_wire(messages),
                    "tools": [],
                    "response": {
                        **result,
                        "request_id": identity.request_id,
                        "step_number": identity.ordinal,
                    },
                }
                if reasoning_effort is not None:
                    call["reasoning_effort"] = reasoning_effort
                payload = encode(call)
            except (ValueError, TypeError, KeyError, OverflowError):
                self.reject(identity)
                return
            self._settled.add(identity.request_id)
            if len(payload) > self.call_byte_limit:
                self.dropped_calls += 1
                self.truncated = True
                return
            self._records.append(CallRecord(identity, payload))
            # Include extension overhead, not just call bytes.
            if len(encode(self.extension())) > self.byte_limit:
                self._records.pop()
                self.dropped_calls += 1
                self.truncated = True

    def extension(self):
        with self._lock:
            extension = {
                "schema_version": SCHEMA_VERSION,
                "capture_point": "backend_model_boundary",
                "execution_generation_id": self.execution_generation_id,
                "capture_byte_limit": self.byte_limit,
                "calls": [
                    json.loads(r.payload)
                    for r in sorted(self._records, key=lambda r: r.identity.ordinal)
                ],
                "truncated": self.truncated,
                "dropped_calls": self.dropped_calls,
                "rejected_calls": self.rejected_calls,
            }
            if len(encode(extension)) > self.byte_limit:
                self.truncated = extension["truncated"] = True
            return extension
