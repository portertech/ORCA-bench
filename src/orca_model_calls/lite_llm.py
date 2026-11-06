"""Harbor adapter using per-call official LiteLLM async success callbacks.

No global callback registration or copied Harbor call implementation. A
condition bridges logging workers and the asyncio loop. Harbor's existing
usage-extraction extension point counts successful raw responses, including
ones retried after conversion errors, so a missing callback cannot hide loss.
"""

import asyncio
import threading
from contextvars import ContextVar
from datetime import datetime, timezone

from harbor.llms.lite_llm import LiteLLM

_observation = ContextVar("orca_model_call_observation", default=None)


def response_key(response):
    # The pinned LiteLLM callback receives the same raw response object.
    return id(response)


class CapturingLiteLLM(LiteLLM):
    def __init__(self, *args, recorder, callback_timeout=10.0, **kwargs):
        if kwargs.get("use_responses_api"):
            raise ValueError("Responses API capture is not supported")
        if callback_timeout <= 0:
            raise ValueError("callback_timeout must be positive")
        self.recorder = recorder
        self.callback_timeout = callback_timeout
        self._capture_call_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)

    def _extract_usage_info(self, response):
        observation = _observation.get()
        if observation is not None:
            condition, expected, identities, recorder = observation
            with condition:
                key = response_key(response)
                expected[key] = response
                if key not in identities:
                    identities[key] = recorder.begin()
        return super()._extract_usage_info(response)

    async def call(self, *args, **kwargs):
        # Terminus calls (including summarization) are sequential. Serialize any
        # same-instance callers to retain request order; separate trials stay concurrent.
        async with self._capture_call_lock:
            return await self._capturing_call(*args, **kwargs)

    async def _capturing_call(self, *args, **kwargs):
        if self._use_responses_api:
            raise ValueError("Responses API capture is not supported")
        condition = threading.Condition()
        # Retain raw objects until settlement so Python object IDs cannot be reused.
        expected, seen = {}, {}
        identities = {}
        closed = False
        recorder = self.recorder  # A late callback cannot enter a later execution.
        token = _observation.set((condition, expected, identities, recorder))

        async def success(details, response, start_time, end_time):
            with condition:
                key = response_key(response)
                if closed or key in seen:
                    return
                seen[key] = response
                if key not in identities:
                    identities[key] = recorder.begin()
                identity = identities[key]
                try:
                    provider = details.get("custom_llm_provider") or details.get(
                        "litellm_params", {}
                    ).get("custom_llm_provider")
                    effort = details.get("optional_params", {}).get(
                        "reasoning_effort", self._reasoning_effort
                    )
                    recorder.record(
                        identity,
                        requested_model=self._model_name,
                        provider=provider,
                        reasoning_effort=effort,
                        messages=details.get("messages"),
                        tools=details.get("optional_params", {}).get("tools"),
                        response=response,
                        # LiteLLM's end_time may be naive local time. A fresh UTC
                        # observation is unambiguous; provider created wins in mapping.
                        completed_at=datetime.now(timezone.utc),
                    )
                except Exception:
                    recorder.reject(identity, "callback_mapping_failed")
                finally:
                    condition.notify_all()

        callbacks = list(
            kwargs.pop("success_callback", self._llm_kwargs.get("success_callback", []))
        )
        kwargs["success_callback"] = [*callbacks, success]

        def wait():
            with condition:
                return condition.wait_for(
                    lambda: expected.keys() <= seen.keys(), self.callback_timeout
                )

        try:
            return await super().call(*args, **kwargs)
        finally:
            try:
                await asyncio.to_thread(wait)
            finally:
                with condition:
                    closed = True
                    for key in expected.keys() - seen.keys():
                        recorder.reject(identities[key], "callback_timeout")
                _observation.reset(token)
