package main

import (
	"encoding/json"
	"os"
	"testing"

	a "github.com/grafana/agento11y/go/agento11y"
)

func nativeFixture(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("../../tests/fixtures/model_calls/valid-native.json")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestNativeProjectionAndSchema(t *testing.T) {
	raw := nativeFixture(t)
	artifact, err := validateArtifact(raw)
	if err != nil {
		t.Fatal(err)
	}
	if artifact.Extra.Calls.incomplete() {
		t.Fatal("complete fixture marked incomplete")
	}
	g := a.Generation{Metadata: map[string]any{}}
	if err := nativeProjection(&g, raw); err != nil {
		t.Fatal(err)
	}
	if len(g.Input) != 1 || len(g.Output) != 3 || len(g.Tools) != 1 || g.SystemPrompt != "Synthetic system prompt" {
		t.Fatal("lost native conversation structure")
	}
	if g.Output[0].Parts[1].ToolCall.ID != "synthetic-tool-1" || g.Output[1].Parts[0].ToolResult.ToolCallID != "synthetic-tool-1" {
		t.Fatal("lost native tool associations")
	}
	if _, err := json.Marshal(g); err != nil {
		t.Fatal(err)
	}
	schema, err := os.ReadFile("../../schemas/llmspec-model-calls-v2.schema.json")
	if err != nil || string(schema) != string(schemaV2Bytes) {
		t.Fatal("v2 embedded schema drift", err)
	}
}

func TestNativeInvalidSemantics(t *testing.T) {
	mutations := []func(map[string]any){
		func(e map[string]any) { e["successful_responses"] = 3 },
		func(e map[string]any) { e["pending_requests"] = 1 },
		func(e map[string]any) {
			c := e["calls"].([]any)[1].(map[string]any)
			c["messages"].([]any)[3].(map[string]any)["tool_call_id"] = "orphan"
		},
	}
	for i, mutate := range mutations {
		var root map[string]any
		if err := json.Unmarshal(nativeFixture(t), &root); err != nil {
			t.Fatal(err)
		}
		mutate(root["extra"].(map[string]any)["llmspec_model_calls"].(map[string]any))
		raw, _ := json.Marshal(root)
		if _, err := validateArtifact(raw); err == nil {
			t.Fatalf("accepted invalid mutation %d", i)
		}
	}
}

func TestNativeUncertaintyIsIncomplete(t *testing.T) {
	for _, e := range []extension{
		{Version: "llmspec.model-calls.v2"},
		{Version: "llmspec.model-calls.v2", Finalized: true, Uncertain: 1},
		{Version: "llmspec.model-calls.v2", Pending: 1},
	} {
		if !e.incomplete() {
			t.Fatal("accepted unfinished or uncertain native capture")
		}
	}
}
