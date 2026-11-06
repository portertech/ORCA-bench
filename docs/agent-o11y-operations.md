# Capture, publish, and verify model-call artifacts

## Security and scope

Capture is opt-in. Use fresh trials in controlled environments. Prompts include
terminal output and can contain secrets. Never commit real captures, export
bundles, dry-run snapshots, receipts, or deployment configuration. Use restrictive
filesystem permissions and an approved staging destination. Audit artifacts before
publishing. Do not redact or rewrite them in place; discard an unsafe run instead.
The producer explicitly selects callback metadata and does not serialize headers,
API keys, or arbitrary callback kwargs. That does not sanitize the prompts.

Ordinary `job-config.yaml`, the existing ORCA config and leaderboard workflows are
unchanged. Python capture requires Harbor 0.20.0 and LiteLLM 1.89.3. The Go tool
requires Go >= 1.25.7 and the public Agent Observability SDK v0.18.0. Keep exact
adapter dependency pins until their integration tests pass against new versions.

## Capture

From the repository root:

```bash
uv sync --locked --group tests
uv run pytest -q tests/orca_model_calls
(cd tools/agento11y-export && go test -race ./... && go vet ./...)
```

The local HTTP integration test uses the real LiteLLM transport without any paid
provider or credentials. It verifies concurrent trial isolation and callback
completion. Per-call **async** success callbacks are required at this pin; plain
per-call synchronous callback functions were not reliably invoked. The adapter
also overrides Harbor's existing `_extract_usage_info(response)` extension point
to count each successful raw response before conversion, including successful
responses followed by a Harbor exception/retry. It waits for all corresponding
callbacks and accounts for missing callbacks rather than treating the first
callback as sufficient. No Harbor source patch or copied `LiteLLM.call` is used.
Each LLM instance's calls are serialized; different trial instances are concurrent.

For an explicitly enabled, tiny paid-provider boundary check:

```bash
ORCA_LIVE_CAPTURE_MODEL=openai/gpt-4o-mini \
  uv run pytest -q -s tests/orca_model_calls/test_live_capture.py
```

Set `ORCA_LIVE_CAPTURE_API_BASE` only for an approved alternative endpoint. Provider
credentials use LiteLLM's normal environment variables. The test keeps the artifact
in memory and prints only counts, size and SHA-256.

For the complete Harbor lifecycle with a tiny synthetic Docker task:

```bash
uv run harbor run -c configs/harbor_job_model_calls_smoke.yaml
```

For Claude Sonnet 5.5 instead, using an ignored `.env` containing
`ANTHROPIC_API_KEY`:

```bash
chmod 600 .env
uv run --env-file .env harbor run \
  -c configs/harbor_job_model_calls_smoke_sonnet.yaml
```

The Sonnet config uses `anthropic/claude-sonnet-5-5` and
`thinking: {type: between_tools}`. This is Sonnet 5.5's documented text-only mode
for requests without native tools; it does not validate adaptive-thinking payload
support. See the [Sonnet 5.5 migration notes](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5#turn-off-up-front-thinking).
`uv run --env-file` loads secrets into the process environment; neither Harbor nor
the Go exporter should be assumed to load `.env` automatically.

These smokes require Docker and the selected provider API key. They are **not ORCA benchmark results**
and cannot establish realistic ORCA artifact sizes. The task only creates a file
containing `OK`; its synthetic reward and details exercise exporter loading.

For a real ORCA pilot, follow the README's snapshot setup, supply
`GRADIENT_API_KEY`, and replace `TASK_NAME` with one exact dataset task:

```bash
./run_harbor_cached.sh -c configs/harbor_job_orca_bench_agent_o11y.yaml \
  --include-task-name TASK_NAME --n-tasks 1 --n-concurrent 1
uv run orca-model-calls jobs/JOB/TRIAL/agent/llmspec-trajectory.atif.json
```

### One released task with Sonnet on Apple Silicon

`configs/harbor_job_orca_bench_agent_o11y_sonnet.yaml` selects one public Hub task
with concurrency one, Sonnet 5.5, and the same text-only thinking mode as the
synthetic smoke. It uses the normal task timeout and resources and does not enable
public leaderboard uploads. It requires both `ANTHROPIC_API_KEY` (agent) and
`OPENAI_API_KEY` (official verifier). The verifier's base URL must not be empty;
Compose otherwise passes an empty string that the OpenAI SDK rejects.

Harbor 0.20.0 hard-codes its home-directory cache. If that location is unavailable,
the optional `patches/harbor_constants.py` adds `HARBOR_CACHE_DIR` while preserving
the original default and registry settings. This is a cache-location patch, not a
capture/LLM adapter patch. Install it into the pinned environment:

```bash
cp patches/harbor_constants.py "$(uv run python -c \
  'import harbor.constants as c; print(c.__file__)')"
export HARBOR_CACHE_DIR="$PWD/.cache/harbor"
export DOCKER_DEFAULT_PLATFORM=linux/amd64
```

The published task and snapshot images are AMD64-only. Restore the declared image:

```bash
docker pull --platform linux/amd64 \
  orcabench/sre-otel-snapshot:data-0418-harbor-template-v2
```

It is roughly 26 GiB compressed and 59 GB unpacked. The staged host snapshot needs
roughly another 55 GiB; check disk space first. A completed run under emulation with Docker
Desktop's 8 GiB allocation is feasible at concurrency one, but is not evidence of
capacity for concurrent trials.

A pre-existing staged snapshot may be reused **read-only**, provided its cache key
matches the first 12 hex characters of the image ID and it contains the task's
named snapshot. Do not reuse agent outputs or reconstruct captures from old jobs.
For an already verified cache, skip extraction and mount the same absolute path
inside and outside the container:

```bash
export SNAPSHOT_CACHE_HOST_DIR=/ABSOLUTE/VERIFIED/CACHE/IMAGE_ID_PREFIX
MOUNTS="$(uv run python -c 'import json, os; p = os.environ["SNAPSHOT_CACHE_HOST_DIR"]; print(json.dumps([{"type": "bind", "source": p, "target": p, "read_only": True}]))')"
uv run --env-file .env bash -c '
  export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"
  exec harbor run "$@"
' harbor-capture --mounts-json "$MOUNTS" \
  -c configs/harbor_job_orca_bench_agent_o11y_sonnet.yaml
```

The verifier base URL is defaulted **after** loading `.env`, preserving an explicitly
configured judge endpoint. Do not copy local cache paths or credentials into a
tracked config. Harbor's ordinary default cache location remains unchanged unless
this optional patch and environment override are explicitly installed.

### Validate a fresh capture

Check that the existing `agent/trajectory.json` remains present. Compare successful
logical API-call counts, including summarization, with the captured call count.
Record bytes and SHA-256 **outside Git**. For a normal successful smoke, require
zero dropped/rejected calls and `truncated=false`. The validator accepts wire-valid
loss diagnostics, length-limited completions, empty text and unknown finish reasons;
that does not mean they are complete captures or suitable for every use.

The dedicated agent accepts `capture_byte_limit` (default 67108864),
`capture_call_byte_limit` (4194304), and `capture_callback_timeout` (10 seconds).
Only whole calls are retained. Local call ordinals are not transcript step IDs.
The agent refuses reuse, existing capture paths, resume, non-LiteLLM backends,
Responses API mode and linear-history splitting. Use a fresh trial directory.

## Audit and dry-run

From `tools/agento11y-export/`:

```bash
go run . --dry-run --harbor-timezone UTC \
  --evaluator-id orca-bench.verifier --evaluator-version EXAMPLE_VERSION \
  --snapshot ../../jobs/JOB/agento11y-audit.json ../../jobs/JOB
```

The evaluator values above are **examples**, not production configuration. Supply
real deployment-owned values via flags or `ORCA_EVALUATOR_ID` and
`ORCA_EVALUATOR_VERSION`. A snapshot contains sensitive generation input/output;
the writer creates it with mode 0600. Ordinary dry-run stdout contains associations,
artifact sizes and hashes, not prompts. No SDK client or network call is created
in dry-run mode. When changing the public schema, update
`tools/agento11y-export/schema.json` from the repository schema; tests require byte
identity. The Python package includes the same schema via its source symlink.

`--trial`, `--task`, `--model` are exact filters. Default grouping is
`(job ID, agent name/version, requested model)`; `--per-model=false` groups models
within the same job/agent. Attempts are chronological within task/model, with trial
ID as a deterministic tie-breaker. Filtering changes the publication snapshot;
use a new `--run-id` rather than reuse a run with changed inputs. `--run-id` requires
a single experiment group; `--name` overrides the human-readable name.

Harbor may emit timezone-naive timestamps. Supply the **actual host timezone**
with `--harbor-timezone` in that case. Missing completed timestamps, negative
usage/cost, conflicting provenance, duplicate trial/execution/artifact identities,
malformed rewards, and contract-invalid artifacts fail closed.

There is exactly one headline verifier score. `--score-key` defaults to `final`;
benchmark `passed` defaults to reward exactly 1.0. `--pass-threshold N` explicitly
switches to reward >= N. The audit of `check_prediction.py` confirms that reward
is mean rubric score / 3 for both incident and control tasks. A perfect control
can report reward 1.0 while `hallucinate_any` is undefined and omitted. Neither
RCA accuracy nor hallucination is an additional pass requirement. Available
RCA/hallucination and judge identity/effort are retained as score metadata.

## Staging publication

Configure secrets using your deployment's secret manager; do not put them in
tracked files. Supported SDK settings include:

| Variable / flag | Purpose |
| --- | --- |
| `AGENTO11Y_ENDPOINT` | SDK ingestion service origin for generations and experiment/trial/score/artifact writes |
| `AGENTO11Y_PROTOCOL` | `http` or `grpc` (not instrumentation-only) |
| `AGENTO11Y_API_ENDPOINT` / `--api-endpoint` | Optional SDK write API origin override; defaults to `AGENTO11Y_ENDPOINT` |
| `AGENTO11Y_CONTROL_ENDPOINT` | Grafana read/control proxy, **not** the SDK write endpoint |
| `AGENTO11Y_INGEST_ACTOR` | SDK ingestion actor header (default `ingest:sdk/go`) |
| `AGENTO11Y_GRAFANA_URL` | Grafana root used for the printed experiment UI URL |
| `AGENTO11Y_SERVICE_ACCOUNT_TOKEN` | Grafana service-account credential for read/export verification; not the SDK ingest credential |
| `AGENTO11Y_AUTH_MODE` | Supported SDK mode: `basic`, `bearer`, `tenant`, `none` |
| `AGENTO11Y_AUTH_TENANT_ID` | Deployment tenant identity where required |
| `AGENTO11Y_AUTH_TOKEN` | Deployment token/password where required |
| `AGENTO11Y_INSECURE` | Explicit insecure transport only for approved local tests |
| `ORCA_EVALUATOR_ID`, `ORCA_EVALUATOR_VERSION` | Required exporter evaluator identity |

The SDK also supports transport/queue/timeouts via its documented settings.
Generation export must not be disabled; the command rejects `none`/OTel-only modes.

`--api-endpoint` takes precedence over `AGENTO11Y_API_ENDPOINT`, which defaults
to `AGENTO11Y_ENDPOINT`. The SDK's write methods use the ingestion API with the
ingestion credentials for experiments, trials, generations, scores and artifacts.
Do **not** point those methods at the Grafana `resources/eval` proxy: its path and
service-account authentication are different. The exporter rejects path-prefixed
write origins rather than allowing the SDK to silently strip the path.

Use Grafana credentials for GCX read/export operations instead. For example, map
`AGENTO11Y_GRAFANA_URL` to GCX's `GRAFANA_SERVER` and
`AGENTO11Y_SERVICE_ACCOUNT_TOKEN` to `GRAFANA_TOKEN` within your secret-loading
workflow. Do not put tokens in command arguments or alter unrelated OAuth contexts.
Credentials alone are not sufficient: also supply an evaluator ID/version and
confirm the intended destination. After publication, the exporter prints
`experiment_url=...`; `AGENTO11Y_EXPERIMENT_URL_TEMPLATE` can override the SDK URL.

To load a root `.env` for an exporter dry-run, run from the repository root:

```bash
uv run --env-file .env go -C tools/agento11y-export run . \
  --dry-run --harbor-timezone HOST_TIMEZONE \
  --evaluator-id DEPLOYMENT_EVALUATOR_ID \
  --evaluator-version DEPLOYMENT_EVALUATOR_VERSION ../../jobs/JOB
```

`HOST_TIMEZONE` is the IANA timezone of the machine that ran Harbor, not
necessarily UTC. Replace the evaluator placeholders with your deployment values.

After a dry-run audit, omit `--dry-run`:

```bash
go run . --harbor-timezone UTC \
  --evaluator-id "$ORCA_EVALUATOR_ID" \
  --evaluator-version "$ORCA_EVALUATOR_VERSION" ../../jobs/JOB
```

Publication order is experiment -> all running trials -> generations -> successful
flush -> scores -> required original-byte artifacts -> terminal trials -> terminal
experiment. Score retries added by the tool are restricted to explicit asynchronous
`generation_not_found`/`generation not found` races. The SDK owns bounded network
and transient HTTP retries. Any required failure marks the experiment failed when
possible. Artifacts use `test_case_trial` parents and exact public name/kind/MIME.

A benchmark failure is not necessarily an execution/capture failure: an executed,
fully captured trial with reward below threshold is terminal `completed`, with
score `passed=false`. Harbor exceptions make trials `failed`. An incomplete capture
is rejected by default. `--allow-incomplete` explicitly permits diagnostic
publication but forces the affected trial and entire experiment to `failed`.

### Idempotency and interrupted uploads

Stable run, trial, generation and score IDs support repeatable publication.
Private local receipts default to `JOB/.agento11y-export` (`--state-dir` overrides).
Keep this directory: it fingerprints the entire plan and records confirmed uploads.
A completed unchanged publication makes no further calls. Confirmed artifacts are
not uploaded again. A changed plan under the same run ID is refused.

Uploads are journaled as `pending` **before** sending. After an ambiguous failure
or crash, the command refuses automatic upload replay. Reconcile the remote
artifact's association and hash first; do not blindly delete receipts or lock files.
For a confirmed remote upload, replace its pending entry with the verified local
artifact SHA-256. If the server definitively has no artifact, remove that pending
entry before retrying. Back up receipts before manual recovery. An interrupted
lock must also be removed only after ensuring no publisher is still running.

The SDK may retransmit an upload within its own HTTP retry policy. Exact-once
server association under such retries remains a **staging idempotency gate**;
local journaling alone cannot prove network exactly-once delivery. Scope receipts
to one destination/tenant and preserve them. Never point this workflow at production
until the gates below pass.

## Upload-size probe (explicit, staging only)

From `tools/agento11y-export/`, with staging SDK environment and a deployment-owned
probe evaluator configured via `ORCA_EVALUATOR_ID` / `ORCA_EVALUATOR_VERSION`:

```bash
ORCA_STAGING_SIZE_PROBE=1 \
ORCA_STAGING_RECEIPT_DIR=/PRIVATE/UNTRACKED/size-probe-receipts \
  go test -run '^TestStagingArtifactSizes$' -v -timeout 20m
```

This publishes clearly named **synthetic, non-benchmark** experiments with valid
16, 32 and 64 MiB artifacts, using the same SDK publisher. Retain the printed run
IDs and private snapshots with expected hashes, then test GCX pulls. The probe is
skipped by default and public CI has no deployment credentials. It is a transport
test, not a measurement of realistic ORCA capture budgets. Do not silently split
an oversized artifact: either raise server limits or version the public contract.

## GCX export and byte identity

```bash
gcx agento11y experiments pull RUN_ID \
  --output-dir /PRIVATE/UNTRACKED/export-1 \
  --include-conversations --include-artifacts
```

GCX requires a new destination directory. Inspect `manifest.json` and download
receipts for errors, and verify the experiment/trial/test-case/score-generation/
conversation/artifact associations. The execution-generation ID must agree across
all relevant records. No ORCA-specific conversion may be applied to the bundle.

Hash the downloaded artifact at the path recorded by GCX and compare with the
original **local artifact bytes**, not a parsed/re-serialized copy:

```bash
shasum -a 256 jobs/JOB/TRIAL/agent/llmspec-trajectory.atif.json
shasum -a 256 /PRIVATE/UNTRACKED/export-1/PATH_FROM_ARTIFACT_RECEIPT
shasum -a 256 /PRIVATE/UNTRACKED/export-1/manifest.json \
  >> /PRIVATE/UNTRACKED/acceptance-receipt.txt
```

Repeat the pull to a new `export-2` directory. Artifact bytes and semantic
associations must be equivalent. Only documented GCX manifest fields intentionally
varying between pulls may differ. Retain both manifest hashes and the run receipt
outside Git. An external compatibility gate may consume the **unmodified** bundle
outside this public repository and report pass/fail plus sanitized incompatibilities.
Do not add private consumer source, commands, policies or configuration here.

## Acceptance status and remaining gates

Implemented and locally exercised:

- Closed public schema, semantic validators, synthetic golden and negative tests.
- Concurrent real LiteLLM HTTP transport tests; timeout/late callback, length,
  conversion-retry accounting and lifecycle failure tests.
- One real paid-provider boundary smoke with zero loss; artifact retained only in
  memory and a size/hash receipt outside Git.
- One complete synthetic Docker/Harbor run: two successful calls, zero loss,
  ordinary and strict trajectories, verifier reward 1.0, Python/Go validation and
  exporter dry-run. Real artifacts remain under ignored `jobs/`, not in fixtures.
- Claude Sonnet 5.5 text-only synthetic Harbor smoke: two calls, zero loss and
  reward 1.0; credential exclusion and exporter dry-run passed. This surfaced and
  fixed the mapping of cached assistant text blocks; the original incomplete
  capture was retained unchanged, and the passing capture came from a fresh run.
- Publication of one passing Sonnet synthetic smoke to an operator-confirmed
  target, with server-side completed status and passing headline score. An
  unmodified GCX pull verified all experiment/trial/generation/score/artifact
  associations, exact artifact byte identity and manifest checksums. The bundle
  and manifest hash receipt remain outside Git.
- Real Go SDK HTTP publication tests for associations, order, exact artifact bytes,
  failure status, score retries and receipt replay protection.

**Not yet established; deployment acceptance is not complete:**

- A realistic ORCA task pilot using the requested Gradient models and snapshot.
- Gradient model/provider identity and representability of its live responses.
- Staging publication of passing and failing ORCA benchmark trials, plus the
  incomplete-capture diagnostic case.
- Staging 16/32/64 MiB uploads and upload-retry server idempotency.
- Repeat-pull equivalence and realistic ORCA GCX byte-integrity acceptance (the
  single synthetic smoke's download bytes and manifest anchor have passed).
- External compatibility gate against unmodified exported bundles.

Keep concurrency and coverage low until these gates pass. A synthetic smoke does
not establish production size support or downstream compatibility.
