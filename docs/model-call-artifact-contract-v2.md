# Native model-call artifact contract (v2)

`llmspec.model-calls.v2` is a separate, closed public contract. Its normative
structural schema is
[`schemas/llmspec-model-calls-v2.schema.json`](../schemas/llmspec-model-calls-v2.schema.json).
The synthetic example is `tests/fixtures/model_calls/valid-native.json`.
[v1](model-call-artifact-contract.md) remains unchanged; consumers must dispatch
versions explicitly rather than interpret v2 messages as v1 text-part arrays.

## Envelope and boundary

The filename remains `agent/llmspec-trajectory.atif.json`. Strict UTF-8 JSON,
no duplicate keys/non-finite numbers, the closed capture envelope, call identities,
response associations, timestamps, whole-call budgets, and publication associations
follow the v1 rules except where this document explicitly changes them.

This envelope is the public model-call capture representation, not a replacement
for Harbor's ordinary `trajectory.json`. In particular, the capture transcript
uses normalized `environment` sources; Harbor ATIF uses agent steps with associated
observations. The native producer emits both files.

The extension uses `capture_point: litellm_chat_boundary`: input is the effective
request **submitted to LiteLLM**, before its provider translation; response is the
selected complete SDK assistant response. No HTTP wire-byte claim is made.
Provider-translated messages/headers and arbitrary callback metadata are not
serialized. The execution's agent name/version is supplied by the producer, not
hard-coded to Terminus.

## Native messages and tools

A call retains v1's request/execution IDs, ordinal/step association, requested and
runtime model/provider, response ID/timestamp, finish reason mapping, and selected
reported token usage. It adds required `request_options` and permits native tools.

`messages` contains ordered native chat objects:

- System and user: `role` and string `content`.
- Assistant: `role: assistant`, `content` as string or null; optional
  `reasoning_content`, `thinking_blocks`, `provider_specific_fields.thinking_blocks`,
  and ordered `tool_calls`.
- Tool result: `role: tool`, string `content`, and `tool_call_id`.

Each native call declaration contains `id`, `type: function`, and a function with
`name` plus the **original argument string**. An optional nonnegative SDK `index`
and `caller: {type: direct}` are preserved when supplied. The argument string is
not parsed/re-serialized for capture, even when it is malformed JSON. Execution
validates it separately and can return an explicit error observation.

Signed thinking blocks have `type: thinking`, `thinking` text and `signature`;
redacted blocks have `type: redacted_thinking` and opaque `data`. Supported fields
remain in their original top-level/provider-specific positions. Nonempty unknown
message fields are rejected, not flattened to text. Empty/null SDK diagnostic
defaults are omitted. The initial producer does not insert manual cache markers;
automatic provider cache usage remains in the reported usage fields.

Native `tools` is an ordered list of OpenAI-style function definitions containing
`type: function` and `function: {name, description, parameters}`. The JSON Schema
object is the exact effective definition exposed to the model, not necessarily
the original unadapted MCP discovery schema. Tool names must be unique. Provider
adaptations must be explicit and versioned rather than silently relaxing schemas.

`request_options` requires `max_tokens` and `tool_choice: auto`. It may include
finite `temperature`, `reasoning_effort`, the supported `thinking` mode, and the
explicit `allowed_openai_params` list for native tools/configured reasoning.
These are selected controls, not a serialization of arbitrary LiteLLM kwargs.
Secrets, callback objects and transport headers are forbidden. Native definitions
and metadata can contain finite fractional numbers; accounting counts, ordinals,
steps, budgets and reported usage use integer JSON notation.

Both validators check each history's native IDs: tool results must match pending
assistant declarations, no declaration ID may be reused within a history, and
all pending results must be settled before the next non-tool message or model
request. The newly returned assistant response may declare pending calls for the
runner to execute after capture. Request and response IDs remain unique within
the execution, independently of tool-call IDs.

## Completeness, uncertainty, and usage

The extension retains `truncated`, `dropped_calls`, `rejected_calls`, and
`capture_byte_limit`, and adds:

- `successful_responses`: the number of successful responses observed by the
  producer; equal to retained calls + dropped calls + rejected calls.
- `pending_requests`: model invocations still awaiting settlement.
- `uncertain_requests`: invocations for which the producer cannot prove complete
  response accounting, including provider/translation exceptions, unsettled
  callbacks or inconsistent callback/return/provider identity.
- `finalized`: execution finalization completed; a finalized artifact cannot have
  pending requests.

A normal complete artifact must be finalized, have no pending/uncertain requests,
no drops/rejections/truncation, and valid associations. Merely passing the schema
is insufficient. A checkpoint left by SIGKILL is diagnostic, never complete.

The initial runner disables retries and aborts on uncertainty rather than
assuming an exception implies that no backend response existed. It cannot claim
wire-level accounting for SDK-internal conversions that fail before exposing a
response. That uncertainty is explicit and blocks normal publication. Known
successful responses remain captured even when an execution subsequently fails.

Whole-call immutable retention and the default 64 MiB extension / 4 MiB call
budgets are unchanged. Explicit tool-result clipping is a runner observation
policy, not permission to truncate model-call records: capture stores exactly the
bounded observation actually passed to the model. No partial retained call may
be presented as complete.

Harbor usage is tallied independently of retained payloads, including known
responses rejected/dropped by capture. Unreported usage or usage after an
uncertain invocation is unavailable rather than an invented zero. LiteLLM's normalized prompt-token count is inclusive of cache buckets; cached
input is a subset, never added again when deriving total tokens. The v2 exporter
sets the public SDK's `TokenInputSemanticsInclusive` explicitly rather than relying
on provider-name heuristics. Price/cost
inference is not part of the model-call contract. Execution failure and partial
benchmark reward are separate from capture completeness.

## Publication and projections

The execution ID remains the SDK generation/conversation ID, and is consistently
associated with the trial, headline score and mandatory original-byte artifact.
Upload order, crash-safe receipts, benchmark pass threshold and GCX byte-identity
requirements follow v1. The exporter rejects unknown versions and normally rejects
incomplete captures. Explicit incomplete diagnostic publication forces failed
trial/experiment state.

The public SDK generation is a **marked conversation projection** with native
function definitions, call/result parts and readable reasoning. It is not the
exact per-call record store. Signed/redacted fields and raw malformed argument
strings remain authoritative in the original artifact. Malformed arguments in
SDK JSON fields have an explicit projection marker; no conversion repairs the
original bytes. Similarly, the ordinary Harbor ATIF projection marks invalid
arguments and retains the raw string rather than silently asserting `{}` was the
original argument object.

Consumers requiring exact native payloads must read the mandatory unmodified
artifact and explicitly implement v2. No ORCA-specific GCX bundle rewrite is
needed, but v1-only consumer compatibility must not be assumed. Private consumer
implementations, acceptance policies, revision pins and deployment configuration
remain outside this repository.
