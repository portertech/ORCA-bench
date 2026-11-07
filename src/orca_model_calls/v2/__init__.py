"""Native chat/function-call capture at the effective LiteLLM input boundary.

No provider wire-byte claim. v1 is intentionally unchanged.
"""
import copy
import json
from datetime import datetime, timezone

from jsonschema import ValidationError

from orca_model_calls.capture import CallRecord, Recorder, encode
from orca_model_calls.messages import Unrepresentable, object_dict, response_to_wire

SCHEMA_VERSION = "llmspec.model-calls.v2"


def native_message(value):
    msg = object_dict(value)
    # LiteLLM supplies empty diagnostic defaults which are not history fields.
    allowed = {"role", "content", "tool_calls", "tool_call_id", "reasoning_content", "thinking_blocks"}
    for key, val in msg.items():
        if key not in allowed and val not in (None, [], {}):
            raise Unrepresentable("unmodeled native message field")
    msg = {k: v for k, v in msg.items() if k in allowed and v is not None}
    msg.setdefault("content", None)
    # Schema validation below is authoritative; never stringify unmodeled data.
    return json.loads(encode(msg))


def native_response(response):
    raw = object_dict(response)
    if len(raw.get("choices", [])) != 1:
        raise Unrepresentable("exactly one choice required")
    message = native_message(raw["choices"][0]["message"])
    proxy = copy.deepcopy(raw)
    proxy["choices"][0]["message"] = {"role": "assistant", "content": ""}
    result = response_to_wire(proxy, datetime.now(timezone.utc))
    result["message"] = message
    return result


def validate_history(messages, *, complete=True):
    pending, seen = set(), set()
    for msg in messages:
        role = msg["role"]
        if role == "tool":
            ident = msg.get("tool_call_id")
            if ident not in pending:
                raise ValueError("orphan/duplicate tool result")
            pending.remove(ident)
        else:
            if pending:
                raise ValueError("missing tool result")
            for tool in msg.get("tool_calls", []):
                ident = tool["id"]
                if ident in seen:
                    raise ValueError("duplicate tool call identity")
                pending.add(ident)
                seen.add(ident)
    if complete and pending:
        raise ValueError("unsettled tool history")


class NativeRecorder(Recorder):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.finalized = False
        self.pending_requests = 0
        self.uncertain_requests = 0
        self.successful_responses = 0

    def extension(self):
        ext = super().extension()
        ext.update(schema_version=SCHEMA_VERSION, capture_point="litellm_chat_boundary",
                   finalized=self.finalized, pending_requests=self.pending_requests,
                   uncertain_requests=self.uncertain_requests,
                   successful_responses=self.successful_responses)
        if len(encode(ext)) > self.byte_limit:
            self.truncated = ext["truncated"] = True
        return ext

    def record_native(self, identity, *, model, options, messages, tools, response):
        with self._lock:
            self.successful_responses += 1
            try:
                result = native_response(response)
                if result["id"] in self._response_ids:
                    raise Unrepresentable("duplicate response ID")
                self._response_ids.add(result["id"])
                call = dict(request_id=identity.request_id,
                            execution_generation_id=identity.execution_generation_id,
                            ordinal=identity.ordinal, step_number=identity.ordinal,
                            requested_model=model, runtime_provider=model.split("/", 1)[0],
                            runtime_model=result["model"],
                            messages=copy.deepcopy(messages), tools=copy.deepcopy(tools),
                            request_options=copy.deepcopy(options),
                            response={**result, "request_id": identity.request_id,
                                      "step_number": identity.ordinal})
                from .validation import validate_call
                validate_call(call)
                payload = encode(call)
            except (ValueError, TypeError, KeyError, OverflowError, ValidationError):
                self.reject(identity)
                return
            self._settled.add(identity.request_id)
            if len(payload) > self.call_byte_limit:
                self.dropped_calls += 1
                self.truncated = True
                return
            self._records.append(CallRecord(identity, payload))
            if len(encode(self.extension())) > self.byte_limit:
                self._records.pop()
                self.dropped_calls += 1
                self.truncated = True
