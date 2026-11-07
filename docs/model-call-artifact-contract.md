# Model-call artifact contract (v1)

This repository owns the public producer contract identified by
`llmspec.model-calls.v1`. The normative structural schema is
[`schemas/llmspec-model-calls-v1.schema.json`](../schemas/llmspec-model-calls-v1.schema.json).
The semantic rules below supplement JSON Schema. The deterministic examples in
`tests/fixtures/model_calls/` contain synthetic data only. Native-tool agents use
the separate [v2 contract](model-call-artifact-contract-v2.md); v1 semantics below
are not widened by that addition.

## Envelope and identities

The file is `agent/llmspec-trajectory.atif.json`, encoded as strict UTF-8 JSON
without non-finite numbers or duplicate object keys. Extension numbers and step
IDs use integer JSON notation, not fractional/exponent notation (for example,
`1`, not `1.0` or `1e0`). Envelope metrics may contain finite floating-point
numbers. The ATIF-v1.7 root is closed:
`schema_version`, `session_id`, `trajectory_id`, `agent`, `steps`, `final_metrics`,
`extra`. `agent` contains only `name` and `version`. Session identity comes from
Terminus; trajectory identity is fresh per run. Transcript sources are `user`,
`agent`, `environment`; system steps become environment steps. Transcript IDs are
unique, positive and strictly increasing. Other ATIF step fields are inert.

`extra.llmspec_model_calls` is a closed extension containing the schema version,
`capture_point: backend_model_boundary`, a fresh `execution_generation_id`, a
positive `capture_byte_limit`, `calls`, and explicit `truncated`, `dropped_calls`,
`rejected_calls`. The bound measures canonical compact JSON extension bytes
(UTF-8, no ASCII escaping), not the envelope or the transport encoding.

Each successful observed completion has a unique `request_id`, the execution ID,
a positive `ordinal`, and `step_number` equal to that ordinal. Ordinals are local
to this execution, **not ATIF transcript step IDs**. Calls are strictly ordered;
lossless captures have consecutive ordinals starting at one. Gaps must be covered
by loss counters. No transcript is used to reconstruct effective prompts.

## Call representation

Each call records `requested_model` (exact Harbor configuration), observed
`runtime_provider`, `runtime_model`, optional `reasoning_effort` (`none`, `low`,
`medium`, `high`, `xhigh`), effective `messages`, `tools`, and `response`.
An unspecified effort is omitted, never invented as `none`.

System message content is a string; user/assistant content is an ordered array
of `{type: text, text: string}` parts. The producer accepts assistant strings
and single-text-block assistant lists (including Harbor's Anthropic cache markers)
without flattening or relocating the part metadata. Assistant reasoning uses
`{type: reasoning, text: string}` before its text part. User text-part lists are
preserved. `cache_control` is preserved on the original message or part under
inert `providerOptions.cache_control` (`type` and optional `ttl` strings, with
unknown cache-control fields rejected). Assistant content is exactly one text
part, optionally preceded by one reasoning part. This initial producer supports no native
tools (`tools: []`), media, tool results, or unmodeled response structures. Such
successful responses are rejected with explicit accounting, not coerced.

The response has a non-empty provider `id`, timezone-qualified RFC3339
`timestamp` (provider creation time, otherwise observed completion time),
`model`, matching `request_id` and `step_number`, an assistant `message`,
`finish_reason`, and `usage`. Response IDs must be unique within an execution;
response model must equal runtime model. The finish reason object preserves
`raw` exactly, with `unified`: `stop` -> `stop`, `length` -> `length`,
`content_filter` -> `content-filter`, `tool_calls`/`function_call` -> `tool-calls`,
otherwise `unknown`. An absent reason is unrepresentable.

`usage.input_tokens` and `usage.output_tokens` contain a reported non-negative
integer `total` or are empty objects. `usage.raw` contains only selected reported
`prompt_tokens`, `completion_tokens`, `total_tokens`, and numeric members of
`prompt_tokens_details`/`completion_tokens_details`. Unknown values are not
estimated. Callback kwargs, HTTP headers and credentials are never serialized.

## Fidelity, loss, and outcomes

Records are immutable serialized complete calls. The default budgets are 64 MiB
per extension and 4 MiB per call. No partial strings/objects are retained.
Oversized successful calls increment `dropped_calls` and set `truncated`;
unrepresentable successful calls or missing success callbacks increment
`rejected_calls`. If even extension overhead exceeds its bound, `truncated` is
true. Provider requests failing without a successful response are not completions.
Loss counters do not describe benchmark results or consumer policy.

Length-limited outputs, empty text and unknown finish reasons are valid when
representable. Imperfect verifier scores are not wire errors. Failed trials may
still carry complete captures. Incomplete captures must never be published as
successful complete experiments.

## Agent Observability associations

Group experiments by Harbor job, agent name/version and requested model by
default. Use ORCA task name as `test_case_id`, Harbor trial UUID as trial ID,
and deterministic chronological attempt numbers within each task/model group.
The extension execution ID is the generation ID, conversation ID, score
generation/conversation ID, and trial conversation ID. Every headline score
also names the exact experiment and trial and test case. Evaluator ID/version
are deployment-supplied. Benchmark `passed` defaults to reward exactly 1.0;
no incident-only metric is required for control trials.

Upload the original artifact bytes exactly once per trial under
`llmspec-trajectory.atif.json`, kind `json`, MIME `application/json`, using the
trial artifact API (`test_case_trial` parent). Required upload failures prevent
experiment completion. GCX exports with `--include-artifacts` must preserve the
bytes and associations. **No ORCA-specific post-export rewrite is required.**

## Versions and boundaries

This is a producer/transport contract, not consumer admission policy. Unknown
contract versions must be rejected rather than guessed. Breaking structure or
meaning changes require a new version and explicit review. External compatibility
checks run outside this repository against unmodified exports. This repository
does not import, invoke, vendor or mirror a private consumer, its parser, policies,
revision pins or deployment configuration. Ordinary Harbor `trajectory.json` is
human-readable evidence, not exact historical backend capture; fresh runs are
required.
