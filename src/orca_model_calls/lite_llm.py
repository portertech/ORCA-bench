"""Harbor adapter using per-call official LiteLLM success callbacks.

No global callback registration or copied Harbor call implementation. The event
bridges LiteLLM's logging worker and the agent asyncio loop.
"""

import asyncio
import threading
from datetime import datetime, timezone

from harbor.llms.base import OutputLengthExceededError
from harbor.llms.lite_llm import LiteLLM


class CapturingLiteLLM(LiteLLM):
    def __init__(self, *args, recorder, callback_timeout=10.0, **kwargs):
        if kwargs.get("use_responses_api"):
            raise ValueError("Responses API capture is not supported")
        if callback_timeout <= 0:
            raise ValueError("callback_timeout must be positive")
        self.recorder = recorder
        self.callback_timeout = callback_timeout
        super().__init__(*args, **kwargs)

    async def call(self, *args, **kwargs):
        if self._use_responses_api:
            raise ValueError("Responses API capture is not supported")
        event = threading.Event()
        lock = threading.Lock()
        seen = set()
        closed = False
        recorder = self.recorder  # Bind to this execution, even if a callback is late.

        async def success(details, response, start_time, end_time):
            nonlocal closed
            with lock:
                if closed:
                    return
                response_id = getattr(response, "id", None)
                if response_id in seen:
                    return
                seen.add(response_id)
                identity = recorder.begin()
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
                        completed_at=(
                            end_time
                            if isinstance(end_time, datetime)
                            else datetime.now(timezone.utc)
                        ),
                    )
                except Exception:
                    recorder.reject(identity, "callback_mapping_failed")
                finally:
                    event.set()

        callbacks = list(kwargs.pop("success_callback", []))
        kwargs["success_callback"] = [*callbacks, success]
        successful = False
        try:
            result = await super().call(*args, **kwargs)
            successful = True
            return result
        except OutputLengthExceededError:
            successful = True  # Harbor raises after a successful provider response.
            raise
        finally:
            # Also allow callbacks for successful responses Harbor could not convert.
            # Failed provider requests themselves never create completion records.
            arrived = await asyncio.to_thread(event.wait, self.callback_timeout)
            with lock:
                closed = True
                if successful and not arrived:
                    recorder.reject(recorder.begin(), "callback_timeout")
