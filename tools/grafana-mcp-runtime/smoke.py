#!/usr/bin/env python3
"""Run one opt-in, officially graded released-task MCP smoke. Never publishes.

Load provider/verifier credentials into the process environment before invoking.
Requires Docker, a staged matching snapshot cache, and the prepared worker bundle.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["sonnet", "qwen"], default="sonnet")
    parser.add_argument("--snapshot-cache", type=Path, required=True)
    parser.add_argument("--harbor-cache", type=Path, default=Path(".cache/harbor"))
    parser.add_argument("--job-name")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    cache = args.snapshot_cache.expanduser().resolve(strict=True)
    env = dict(os.environ)
    env["SNAPSHOT_CACHE_HOST_DIR"] = str(cache)
    env["HARBOR_CACHE_DIR"] = str(args.harbor_cache.resolve())
    env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
    env["OPENAI_BASE_URL"] = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    env["FIREWORKS_AI_API_KEY"] = env.get("FIREWORKS_AI_API_KEY") or env.get(
        "FIREWORKS_API_KEY", ""
    )
    required = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY" if args.model == "sonnet" else "FIREWORKS_AI_API_KEY",
    ]
    for key in required:
        if not env.get(key):
            parser.error(f"missing {key}")
    # Do not let an unpatched Harbor silently use/download to a different cache.
    checked = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from pathlib import Path; from harbor.constants import CACHE_DIR; assert CACHE_DIR == Path(os.environ['HARBOR_CACHE_DIR'])",
        ],
        env=env,
        capture_output=True,
    )
    if checked.returncode:
        parser.error(
            "install the optional Harbor constants patch (see runbook) before using --harbor-cache"
        )
    image = "orcabench/sre-otel-snapshot:data-0418-harbor-template-v2"
    identity = subprocess.check_output(
        ["docker", "image", "inspect", image, "-f", "{{.Id}}"], env=env, text=True
    ).strip()
    if (
        cache.name != identity.removeprefix("sha256:")[:12]
        or not (cache / "data/prometheus/snapshots").is_dir()
    ):
        parser.error("snapshot cache does not match the locally staged image")
    job = (
        args.job_name or f"orca-grafana-mcp-{args.model}-{datetime.now():%Y%m%d-%H%M%S}"
    )
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job):
        parser.error(
            "job name must contain only letters, digits, underscores and hyphens"
        )
    if (root / "jobs" / job).exists():
        parser.error("smokes require a fresh job name")
    suffix = "sonnet" if args.model == "sonnet" else "fireworks_qwen3p8_max"
    config = f"configs/harbor_job_orca_bench_grafana_mcp_{suffix}.yaml"
    mounts = json.dumps(
        [
            {
                "type": "bind",
                "source": str(cache),
                "target": str(cache),
                "read_only": True,
            }
        ]
    )
    result = subprocess.run(
        ["harbor", "run", "--mounts-json", mounts, "-c", config, "--job-name", job],
        env=env,
    )
    if result.returncode:
        return result.returncode
    results = list((root / "jobs" / job).glob("*/result.json"))
    if len(results) != 1:
        print("Smoke failed: expected one finished trial", file=sys.stderr)
        return 1
    trial = results[0].parent
    data = json.loads(results[0].read_text())
    if data.get("exception_info"):
        print(
            "Smoke failed: Harbor agent/verifier exception; inspect private job logs",
            file=sys.stderr,
        )
        return 1
    from orca_model_calls.atif import loads, validate

    artifact = validate(
        loads((trial / "agent/llmspec-trajectory.atif.json").read_bytes())
    )
    ext = artifact["extra"]["llmspec_model_calls"]
    calls = json.loads((trial / "agent/mcp-invocations.json").read_text())
    complete = ext["finalized"] and not any(
        ext[k]
        for k in (
            "pending_requests",
            "uncertain_requests",
            "rejected_calls",
            "dropped_calls",
            "truncated",
        )
    )
    errors = sum(bool(c.get("is_error")) for c in calls)
    model_mcp = [
        call
        for call in calls
        if not call["tool_call_id"].startswith("bootstrap-")
        and not call.get("is_error")
    ]
    if not complete or not model_mcp:
        print(
            "Smoke failed: incomplete capture or no successful MCP invocation",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "job": job,
                "reward": data.get("verifier_result", {}).get("rewards"),
                "model_calls": len(ext["calls"]),
                "mcp_calls": len(calls),
                "mcp_errors": errors,
                "mcp_tools": sorted({c["mcp_tool"] for c in calls}),
                "capture_complete": complete,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
