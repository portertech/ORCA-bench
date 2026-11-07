"""Direct LiteLLM boundary with settled per-call success callbacks; no retries."""

import asyncio
import copy
import threading

from orca_model_calls.v2 import native_message


async def complete(
    recorder,
    model,
    messages,
    tools,
    options,
    checkpoint,
    *,
    completion=None,
    callback_timeout=10,
):
    if completion is None:
        import litellm

        completion = litellm.acompletion
    identity = recorder.begin()
    recorder.pending_requests += 1
    checkpoint()
    condition = threading.Condition()
    observed = {}
    providers = {}
    closed = False
    returned = None
    snapshot = copy.deepcopy(messages)
    definitions = copy.deepcopy(tools)

    async def success(details, response, start_time, end_time):
        with condition:
            if not closed:
                observed[id(response)] = response
                providers[id(response)] = details.get(
                    "custom_llm_provider"
                ) or details.get("litellm_params", {}).get("custom_llm_provider")
                condition.notify_all()

    failed = False
    try:
        returned = await completion(
            model=model,
            messages=copy.deepcopy(snapshot),
            tools=copy.deepcopy(definitions),
            **options,
            num_retries=0,
            timeout=180,
            success_callback=[success],
        )
        await asyncio.to_thread(lambda: _wait(condition, observed, callback_timeout))
    except BaseException:
        failed = True
        raise
    finally:
        with condition:
            closed = True
            uncertain = failed
            if returned is not None and id(returned) not in observed:
                # Retain the known response, but never claim callback completeness.
                observed[id(returned)] = returned
                uncertain = True
            if failed or len(observed) != 1:
                # A provider/translation error can conceal an HTTP success. Abort;
                # without lower-level proof this is always diagnostic-only capture.
                uncertain = True
            for index, response in enumerate(observed.values()):
                if providers.get(id(response)) != model.split("/", 1)[0]:
                    uncertain = True
                recorder.record_native(
                    identity if index == 0 else recorder.begin(),
                    model=model,
                    options=options,
                    messages=snapshot,
                    tools=definitions,
                    response=response,
                )
            recorder.uncertain_requests += int(uncertain)
            recorder.pending_requests -= 1
        checkpoint()
    if recorder.rejected_calls or recorder.dropped_calls or recorder.uncertain_requests:
        raise ValueError("incomplete native model-call capture")
    if returned.choices[0].finish_reason not in {"stop", "tool_calls"}:
        raise ValueError("model stopped without a complete answer or tool call")
    return native_message(returned.choices[0].message)


def _wait(condition, observed, timeout):
    with condition:
        return condition.wait_for(lambda: bool(observed), timeout)
