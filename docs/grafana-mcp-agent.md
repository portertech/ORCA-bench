# Native Grafana MCP agent (opt-in)

This is an independent implementation of a small Python/LiteLLM native-tool
agent. No o11y-bench implementation or prompts are copied. It targets Harbor
0.20.0; Terminus and its v1 contract remain unchanged.

## Runtime decision

The worker uses Linux AMD64 / Python 3.13, LiteLLM **1.89.3**, MCP SDK **2.3.0**,
and jsonschema **4.26.0**, isolated from the host environment. All transitive
worker requirements are pinned with hashes in
`tools/grafana-mcp-runtime/requirements.lock`. MCP 2.x uses snake_case Python
attributes; it interoperates with the pinned Go server's MCP wire protocol.
The real SDK stdio discovery has been tested inside the published task image.

mcp-grafana **v2.2.0** is downloaded from its public release and verified against
the pinned archive SHA-256. The archive's Apache-2.0 license and README are
retained in the local runtime bundle. The Python SDK and LiteLLM are MIT-licensed;
review transitive package license/notice requirements before redistributing a
prebuilt worker. No third-party binary or wheel is checked into this repository.

Prepare the runtime **before** launching Harbor (requires Python 3.13 with pip):

```bash
python3.13 tools/grafana-mcp-runtime/prepare.py
```

This downloads only public dependencies. The default destination is ignored:
`.cache/grafana-mcp-runtime/linux-amd64`. The measured agent does not resolve
packages or download binaries from the network. Do not change the host's Harbor
or LiteLLM pins to install this worker.

## Capture boundary

`llmspec.model-calls.v2` preserves effective LiteLLM input messages, native tools,
request controls, and returned assistant messages including call IDs, exact
argument strings, reasoning, and supported signed thinking blocks. It does not
claim to preserve provider-translated HTTP bytes. Its self-contained public schema
is `schemas/llmspec-model-calls-v2.schema.json`; v1 semantics are unchanged.

A non-finalized checkpoint, uncertain request, dropped/rejected successful
response, or pending request is incomplete diagnostic data. Provider errors are
not silently retried; uncertainty must not be published as complete capture.
Model tool-argument JSON may be malformed: execution returns an error but capture
preserves the original string. Tool output bounding is explicit in the actual
observation sent to the model; model-call payloads are never partially retained.

Keep generated jobs, captures, invocation logs, runtime bundles, and exports
private and Git-ignored. Tool data is untrusted and can contain sensitive text.
