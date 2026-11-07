package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Explicit, credentialed cache-semantics gate. No model provider is called.
func TestStagingNativeTokenSemantics(t *testing.T) {
	if os.Getenv("ORCA_STAGING_NATIVE_TOKEN_PROBE") != "1" {
		t.Skip("set ORCA_STAGING_NATIVE_TOKEN_PROBE=1 and staging SDK environment explicitly")
	}
	receipts := os.Getenv("ORCA_STAGING_RECEIPT_DIR")
	if receipts == "" {
		t.Fatal("ORCA_STAGING_RECEIPT_DIR must name an untracked private directory")
	}
	dir := syntheticJob(t)
	nonce := fmt.Sprint(time.Now().UnixNano())
	id, executionID := stableID("synthetic-native-trial", nonce), stableID("synthetic-native-generation", nonce)
	var artifact map[string]any
	if err := json.Unmarshal(nativeFixture(t), &artifact); err != nil {
		t.Fatal(err)
	}
	ext := artifact["extra"].(map[string]any)["llmspec_model_calls"].(map[string]any)
	ext["execution_generation_id"] = executionID
	for _, raw := range ext["calls"].([]any) {
		call := raw.(map[string]any)
		call["execution_generation_id"] = executionID
		call["requested_model"] = "synthetic/requested"
		call["response"].(map[string]any)["usage"] = map[string]any{
			"input_tokens": map[string]any{"total": 10}, "output_tokens": map[string]any{"total": 2},
			"raw": map[string]any{"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
				"prompt_tokens_details": map[string]any{"cached_tokens": 4}},
		}
	}
	artifact["final_metrics"] = map[string]any{"total_prompt_tokens": 20, "total_completion_tokens": 4, "total_cached_tokens": 8, "total_llm_calls": 2}
	writeJSON(t, filepath.Join(dir, "trial/agent", artifactName), artifact)
	mutateJSON(t, filepath.Join(dir, "trial/agent/trajectory.json"), func(v map[string]any) { v["session_id"] = artifact["session_id"] })
	now := time.Now().UTC()
	mutateJSON(t, filepath.Join(dir, "trial/result.json"), func(v map[string]any) {
		v["id"], v["task_name"] = id, "synthetic-native-token-probe-not-benchmark"
		v["config"].(map[string]any)["job_id"] = id
		info := v["agent_info"].(map[string]any)
		info["name"], info["version"] = artifact["agent"].(map[string]any)["name"], artifact["agent"].(map[string]any)["version"]
		v["agent_result"] = map[string]any{"n_input_tokens": 20, "n_output_tokens": 4, "n_cache_tokens": 8}
		v["started_at"], v["finished_at"] = now.Add(-time.Minute).Format(time.RFC3339Nano), now.Format(time.RFC3339Nano)
		v["agent_execution"] = map[string]any{"started_at": now.Add(-50 * time.Second).Format(time.RFC3339Nano), "finished_at": now.Add(-10 * time.Second).Format(time.RFC3339Nano)}
	})
	mutateJSON(t, filepath.Join(dir, "trial/config.json"), func(v map[string]any) { v["job_id"] = id })
	snapshot := filepath.Join(receipts, id+".snapshot.json")
	if err := run(t.Context(), []string{"--name", "SYNTHETIC native inclusive token probe (not benchmark)", "--state-dir", filepath.Join(receipts, "state"), "--snapshot", snapshot, dir}); err != nil {
		t.Fatal(err)
	}
	t.Logf("native probe input=20 output=4 cache=8; expected total=24 (not 32); retain snapshot %s for GCX verification", snapshot)
}

// Explicit, credentialed transport-size gate. These are synthetic probe
// experiments, NOT benchmark results and NOT a substitute for a real ORCA pilot.
func TestStagingArtifactSizes(t *testing.T) {
	if os.Getenv("ORCA_STAGING_SIZE_PROBE") != "1" {
		t.Skip("set ORCA_STAGING_SIZE_PROBE=1 and staging SDK environment explicitly")
	}
	receipts := os.Getenv("ORCA_STAGING_RECEIPT_DIR")
	if receipts == "" {
		t.Fatal("ORCA_STAGING_RECEIPT_DIR must name an untracked private directory")
	}
	for _, mib := range []int{16, 32, 64} {
		t.Run(fmt.Sprint(mib), func(t *testing.T) {
			dir := syntheticJob(t)
			id := stableID("synthetic-size-probe", fmt.Sprint(mib), fmt.Sprint(time.Now().UnixNano()))
			executionID := stableID("synthetic-size-generation", id)
			mutateJSON(t, filepath.Join(dir, "trial/result.json"), func(v map[string]any) {
				v["id"] = id
				v["task_name"] = "synthetic-transport-size-probe"
				v["config"].(map[string]any)["job_id"] = id
			})
			mutateJSON(t, filepath.Join(dir, "trial/config.json"), func(v map[string]any) { v["job_id"] = id })
			path := filepath.Join(dir, "trial/agent", artifactName)
			mutateJSON(t, path, func(v map[string]any) {
				ext := v["extra"].(map[string]any)["llmspec_model_calls"].(map[string]any)
				ext["execution_generation_id"] = executionID
				ext["capture_byte_limit"] = mib * 1024 * 1024
				call := ext["calls"].([]any)[0].(map[string]any)
				call["execution_generation_id"] = executionID
				call["messages"] = []any{map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": ""}}}}
				raw, err := json.Marshal(v)
				if err != nil {
					t.Fatal(err)
				}
				part := call["messages"].([]any)[0].(map[string]any)["content"].([]any)[0].(map[string]any)
				part["text"] = strings.Repeat("x", mib*1024*1024-len(raw))
			})
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if len(raw) != mib*1024*1024 {
				t.Fatalf("wrong probe size: %d", len(raw))
			}
			snapshot := filepath.Join(receipts, id+".snapshot.json")
			if err = run(t.Context(), []string{"--name", "SYNTHETIC transport size probe (not benchmark)", "--state-dir", filepath.Join(receipts, "state"), "--snapshot", snapshot, dir}); err != nil {
				t.Fatal(err)
			}
			t.Logf("uploaded %d MiB sha256=%s; retain snapshot %s for GCX verification", mib, digest(raw), snapshot)
		})
	}
}
