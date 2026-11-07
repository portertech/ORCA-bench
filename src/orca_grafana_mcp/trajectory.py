"""Harbor ATIF projection, separate from the exact native capture artifact."""

import copy

from orca_model_calls.atif import loads


def project(artifact, model, tools):
    result = {
        key: copy.deepcopy(value) for key, value in artifact.items() if key != "extra"
    }
    result["agent"].update(model_name=model, tool_definitions=tools)
    original = result["steps"]
    result["steps"] = []
    for step in original:
        if step["source"] == "environment" and step.get("extra", {}).get(
            "tool_call_id"
        ):
            parent = result["steps"][-1]
            parent.setdefault("observation", {"results": []})["results"].append(
                {
                    "source_call_id": step["extra"]["tool_call_id"],
                    "content": step["message"],
                }
            )
            continue
        if step["source"] == "environment":
            step["source"] = "system"
        message = step.get("extra", {}).get("native_message")
        if message:
            step["llm_call_count"] = 1
            if message.get("reasoning_content"):
                step["reasoning_content"] = message["reasoning_content"]
            for call in message.get("tool_calls", []):
                extra = {}
                try:
                    arguments = loads(call["function"]["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("nonobject arguments")
                except ValueError:
                    arguments = {}
                    extra = {
                        "arguments_invalid": True,
                        "raw_arguments": call["function"]["arguments"],
                    }
                tool = {
                    "tool_call_id": call["id"],
                    "function_name": call["function"]["name"],
                    "arguments": arguments,
                }
                if extra:
                    tool["extra"] = extra
                step.setdefault("tool_calls", []).append(tool)
        step["step_id"] = len(result["steps"]) + 1
        result["steps"].append(step)
    metrics = artifact["final_metrics"]
    result["final_metrics"] = {
        "total_prompt_tokens": metrics.get("n_input_tokens"),
        "total_completion_tokens": metrics.get("n_output_tokens"),
        "total_cached_tokens": metrics.get("n_cache_tokens"),
        "total_cost_usd": metrics.get("cost_usd"),
        "total_steps": len(result["steps"]),
    }
    return result
