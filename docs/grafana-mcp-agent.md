# Native Grafana MCP agent (opt-in)

`orca_grafana_mcp.agent:GrafanaMCPAgent` is a separate Harbor agent with native
function calling. It discovers and executes real MCP tools; it does not merely
append server information to a prompt. Terminus, existing job configs, v1 capture,
and normal leaderboard commands remain unchanged by default.

The implementation is independently authored using public SDK APIs. No
implementation or prompts from the AGPL-licensed o11y-bench agent are copied.

## Architecture

```text
Harbor 0.20.0 wrapper
  -> non-root Python worker inside the task container
       -> LiteLLM -> Anthropic / Fireworks
       -> official MCP SDK -> per-trial mcp-grafana child over stdio
                              -> task-local Grafana /grafana API
                                   -> Prometheus / OpenSearch / Jaeger
       -> bounded read-only service source tools
       -> /app/report.md + Harbor ATIF + exact native-call artifact
```

The MCP client and server are both inside the task container. No host port or
shared remote MCP server is needed. The task's readiness gate exports
`GRAFANA_URL=http://frontend-proxy:8080/grafana`; Grafana is **not** the agent's
`localhost:3000`. Other Grafana endpoints are rejected; custom MCP servers are
not supported. Do not supply `agents[].mcp_servers` to this agent: it manages its own
pinned, trial-local stdio subprocess.

## Reproducible runtime

The worker uses Linux AMD64 / Python 3.13, LiteLLM **1.89.3**, MCP SDK **2.3.0**,
and jsonschema **4.26.0**, isolated from the host. All transitive requirements are
pinned with hashes in `tools/grafana-mcp-runtime/requirements.lock`. Harbor and
Terminus's dependency pins are not upgraded to install the worker.

mcp-grafana **v2.2.0** is downloaded from its public release and checked against
the pinned archive SHA-256. The wrapper also verifies the extracted binary's
pinned digest and every runtime manifest entry before upload. The archive's
Apache-2.0 license and README are retained. The Python MCP SDK and LiteLLM core
are MIT-licensed; review transitive license/notice obligations before distributing
prebuilt bundles. Binaries, wheels, environments and captures are never committed.

Prepare the runtime **before** invoking Harbor. Requires Python 3.13 with pip:

```bash
python3.13 tools/grafana-mcp-runtime/prepare.py
```

For a uv-managed preparation environment without system pip:

```bash
uv venv --python 3.13 .cache/mcp-prepare
uv pip install --python .cache/mcp-prepare/bin/python pip==26.0.1
.cache/mcp-prepare/bin/python tools/grafana-mcp-runtime/prepare.py
```

The default output is `.cache/grafana-mcp-runtime/linux-amd64`. Preparation can
run on macOS; wheels and the server are selected for the **container**, not the
host architecture. Runtime setup installs from the verified local wheelhouse
with `--no-index --require-hashes`; there is no per-trial unpinned download.

The worker uses LiteLLM's bundled model catalog rather than downloading mutable
pricing/capability data. That catalog predates the pilot model names. A recorded,
explicit `allowed_openai_params` list permits the preflighted native tool and
configured reasoning controls; controls are **not silently dropped**. Local HTTP
transport tests assert that Fireworks receives tools, tool choice, and reasoning
effort, and that Anthropic signed thinking survives the next turn. Neither an
old catalog nor absent pricing makes a paid model free: cost remains unavailable
(`null`) unless separately established from provider billing.

## Run one officially graded smoke

These are fresh, paid **released ORCA task** runs, not tiny synthetic tasks. They
use one trial at a time, normal task timeouts/resources, and the official verifier.
No leaderboard upload or Agent Observability publication is automatic.

1. Follow the snapshot/cache instructions in
   [the existing capture runbook](agent-o11y-operations.md#one-released-task-with-sonnet-on-apple-silicon).
   Install the optional Harbor constants patch for repository-local caching and
   the existing task-checksum patch described in the README. Stage the declared
   AMD64 snapshot image and a matching host cache. A previously verified staged
   cache may be reused read-only; never reuse agent answers or reconstruct captures.
2. Prepare the worker bundle as above.
3. Supply `OPENAI_API_KEY` for the independent official judge, plus the selected
   agent provider key. Keep `.env` ignored and mode 600; never place keys in YAML
   or command arguments. The launcher defaults the verifier base URL **after**
   loading `.env`, preserving an explicitly configured nonempty endpoint.
4. From the repository root, using an **absolute** snapshot cache path:

```bash
# Sonnet 5.5; requires ANTHROPIC_API_KEY and OPENAI_API_KEY.
uv run --env-file .env python tools/grafana-mcp-runtime/smoke.py \
  --model sonnet --snapshot-cache /ABSOLUTE/CACHE/IMAGE_ID_PREFIX

# Fireworks Qwen3.8 Max; requires FIREWORKS_API_KEY (or FIREWORKS_AI_API_KEY)
# and OPENAI_API_KEY. The alias is made only inside the launcher process.
uv run --env-file .env python tools/grafana-mcp-runtime/smoke.py \
  --model qwen --snapshot-cache /ABSOLUTE/CACHE/IMAGE_ID_PREFIX
```

`--job-name` optionally supplies a new name; existing job directories are refused.
`--harbor-cache` defaults to `.cache/harbor` and requires the optional constants
patch. The launcher checks the local image/cache identity, sets
`DOCKER_DEFAULT_PLATFORM=linux/amd64`, mounts the cache read-only at the same
absolute path, and invokes the corresponding tracked config:

- `configs/harbor_job_orca_bench_grafana_mcp_sonnet.yaml`
- `configs/harbor_job_orca_bench_grafana_mcp_fireworks_qwen3p8_max.yaml`

Both select public task `orca-bench/05216f608f940a48@latest`. Sonnet uses
`thinking: {type: between_tools}`. Fireworks uses medium reasoning and temperature
1. Both use 16384 maximum output tokens, 100 agent turns, and the existing
64 MiB extension / 4 MiB per-call budgets. Advanced direct Harbor runs can set
`runtime_dir` and these limits as agent kwargs; no task resource override is
needed.

Harbor may exit zero when every trial failed. The smoke launcher additionally
checks the trial exception, artifact validation/completeness, and a successful
**model-issued** MCP call (bootstrap discovery alone does not count). A legitimate
partial-credit reward is printed unchanged; it does not cause a fake benchmark
pass or imply failed capture. The normal benchmark pass threshold remains 1.0.

## Tools, data, and isolation

The MCP subprocess is started with:

```text
--transport stdio --disable-write
--enabled-tools search,datasource,dashboard,prometheus,elasticsearch,api
--usage-stats=disabled
```

A second client-side allowlist exposes only required read/query tools. Policy
`query-only-v2` additionally confines typed dashboard/folder UIDs, datasource names
and Prometheus label path segments, including encoded traversal attempts; it does
not rely on upstream URL escaping. The runner verifies the trial's datasource
UIDs and types before making paid model calls:

| UID | Type and usage |
| --- | --- |
| `webstore-metrics` | Prometheus discovery and bounded instant/range queries |
| `webstore-logs` | OpenSearch, `otel-logs-*`, configured `observedTimestamp` field |
| `webstore-traces` | Jaeger through Grafana's datasource proxy, not Tempo |

Prometheus queries are executed by the server through Grafana, including its
read-only `/api/ds/query` POST path. OpenSearch tools require the optional
`elasticsearch` category and use **Lucene**, not Elasticsearch JSON DSL, for this
plugin. Incident queries must use absolute historical RFC3339 timestamps; relative
`now` queries, a rebased scenario clock, wrong data sources, oversized limits and
external project options are rejected.

Unrestricted `grafana_api_request` is **not** exposed to the model. Typed
`jaeger_services`, `jaeger_search` and `jaeger_trace` wrappers build only fixed
GET routes under `/api/datasources/proxy/uid/webstore-traces/api/`. Model arguments
cannot supply HTTP headers, arbitrary methods, external hosts, or admin routes.
The pinned Grafana/plugin stack must support that proxy path; no claim is made
that Tempo tools work against Jaeger.

Source tools are bounded, read-only and rooted at `/app/opentelemetry-demo/src`.
They reject traversal/symlink escapes, hidden files, verifier/test/solution paths,
credential files and process/host resources. There is no unrestricted shell or
arbitrary file-write tool. The final Markdown answer is checkpointed in the
agent log directory, then written and flushed to the task's pre-created
`/app/report.md`. Since `/app` is root-owned, replacing that file by rename is not
possible as the agent user; task permissions are not widened to do so.

The stock snapshot currently permits anonymous **Admin** Grafana access. This
pilot explicitly records that identity and restricts tool execution; it does
**not** claim Viewer RBAC or a hardened sandbox. A least-privilege trial-local
credential is a separate hardening step. Never use publication-destination or
production Grafana credentials here. The worker clears unrelated inherited
credentials, and its MCP child does not receive the model-provider key.

## Artifacts and exact capture

Each trial retains private files under `agent/`:

- `trajectory.json`: ordinary Harbor ATIF-v1.7 with native calls, associated
  observations, and standard aggregate metrics; validated by Harbor's model in
  tests. Invalid argument JSON has an explicitly marked projection rather than
  pretending the argument object was valid.
- `llmspec-trajectory.atif.json`: the public v2 model-call envelope, using the
  same mandatory artifact filename as v1. Use `trajectory.json` for Harbor's own
  ATIF reader; the capture envelope follows its separately versioned schema.
- `mcp-invocations.json`: actual MCP tool names/arguments, model call IDs and error
  flags. `bootstrap-*` IDs distinguish setup discovery from model-issued calls.
- `runtime.json`: dependency/server versions, policy, exact model controls,
  tool fingerprint and source digest; no credentials.
- `metrics.json`, `execution.json`, `final-report.md`, and private runtime logs.
  Failures also get a sanitized `failure.json` containing types/status codes only.

`llmspec.model-calls.v2` preserves effective **LiteLLM input** messages, native
function definitions/JSON Schemas, selected request controls, returned assistant
messages, exact argument strings, IDs, usage, reasoning and supported signed or
redacted thinking blocks. It is not a claim to capture provider-translated HTTP
wire bytes. v1 remains unchanged and unknown versions fail closed.

Per-call async callbacks settle against returned response/provider identities.
Retries are disabled. A translation/provider exception that could conceal a raw
success marks uncertainty and aborts, rather than fabricating completeness.
Mapping rejection, budget loss or callback uncertainty also stops the runner.
Usage is accounted independently of retained payloads; unknown usage/cost stays
unavailable. Immutable whole calls and pending/finalization state are checkpointed
atomically, so a killed worker cannot leave an apparently finalized complete run.
An exclusive persistent execution marker also refuses reuse after a kill before
the first checkpoint. Agent-instance reuse and multi-step execution are rejected;
start a fresh trial instead of overwriting a capture.

Validate privately:

```bash
uv run orca-model-calls jobs/JOB/TRIAL/agent/llmspec-trajectory.atif.json
```

Wire-valid diagnostics are not necessarily complete. For normal publication
require finalized=true, pending=0, uncertain=0, dropped=0, rejected=0 and no
truncation. Scan actual captures for credentials and sensitive content; discard
unsafe runs instead of rewriting original bytes.

## Publication

Follow [the existing publication runbook](agent-o11y-operations.md#staging-publication)
with an operator-approved destination and deployment-owned evaluator identity.
The exporter dispatches v1/v2 explicitly and embeds both public schemas.

For v2 it emits SDK-native tool definitions/calls/results and reasoning parts as
a **marked conversation view**. Signed/redacted provider fields and raw malformed
argument strings remain authoritative in the mandatory **original-byte artifact**;
the SDK view is not a replacement for exact per-call records. Existing stable
trial/generation/conversation/score associations, ordered uploads, failed
incomplete diagnostics and crash-safe receipts are preserved. Never delete or
replay receipts just to test a change.

Do not assume external v1-only consumers support v2. Consumer compatibility and
larger staging probes remain separate acceptance work; private consumer code,
parser policy, pins and deployment configuration do not belong in this repository.

## Live integration status

Fresh, sequential released-task pilots exercised actual Prometheus, OpenSearch
and Jaeger calls through the pinned server, with no Harbor exception:

| Model | Official reward | RCA accuracy | Model calls | Model-issued MCP calls | MCP errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet 5.5 | 0.8333 | 1 | 14 | 28 | 0 |
| Fireworks Qwen3.8 Max | 0.5 | 0 | 34 | 43 | 0 |

Bootstrap discovery adds one MCP call per trial. Both captures finalized with no
pending/uncertain requests, drops, rejections or truncation. Original artifacts
were approximately 2.93 MB and 7.27 MB; largest complete calls were under 0.4 MB.
Reported input/output/cache usage matched Harbor and ordinary ATIF validated.
Both rewards are legitimate **partial credit**, not full benchmark passes. Pilot
cost remains unresolved, not zero; in particular this loop does not yet optimize
Anthropic manual prompt caching. Review actual billing before scaling.

Publication/readback verified all nine inventoried files per bundle, exact
original artifact bytes, score associations, tool definitions, and native
call/result IDs/content (34 and 47 pairs respectively). These are integration
checks, not external v2 consumer acceptance. The separate small staging token
probe verifies the SDK's inclusive-cache flag without making paid model calls;
see [the probe runbook](agent-o11y-operations.md#native-cached-token-probe-explicit-staging-only).
Exporter fixes are not retroactive, and existing receipts must not be discarded.

Remaining deployment/research gates include least-privilege Viewer credentials,
explicit worker/server peak-RSS profiling, large artifact transport/idempotency
probes, staged incomplete-diagnostic publication, private consumer v2 compatibility,
and matched-agent MCP controls.

## Tests and limitations

```bash
uv run --group tests pytest -q tests/orca_model_calls tests/orca_grafana_mcp
go -C tools/agento11y-export test -race ./...
go -C tools/agento11y-export vet ./...
uv build --wheel
```

Default tests use synthetic data and local HTTP transports, including real
LiteLLM Anthropic, Fireworks and OpenAI translation/callback paths. They need no
paid keys or snapshot. The protocol integration test is separately gated by
`ORCA_GRAFANA_MCP_BINARY` and an installed MCP SDK; CI prepares the pinned server
and isolated worker on Linux AMD64 and queries a synthetic Grafana HTTP service
through the actual Go server + official SDK. It verifies `/grafana` subpath
handling, datasource discovery, Prometheus's Grafana query path, Jaeger proxy
routing and subprocess cleanup. No paid network test is enabled in default CI.

Initial limitations: only the two pilot provider configurations have live
preflight coverage; tools execute sequentially; no resume, summarization,
media/Responses API, arbitrary servers, write tools or remote MCP authentication.
Provider-specific payloads outside the closed v2 schema fail explicitly.

A higher score than Terminus does not demonstrate an MCP benefit: the agent also
changed. Causal comparison needs a matched-agent control with equal telemetry
access; a tool-less calibration is not that control. One released-task smoke is
an integration check, not a leaderboard claim.
