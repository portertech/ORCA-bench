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
			id := fmt.Sprintf("synthetic-size-probe-%d-%d", mib, time.Now().UnixNano())
			mutateJSON(t, filepath.Join(dir, "trial/result.json"), func(v map[string]any) {
				v["id"] = id
				v["task_name"] = "synthetic-transport-size-probe"
				v["config"].(map[string]any)["job_id"] = id
			})
			mutateJSON(t, filepath.Join(dir, "trial/config.json"), func(v map[string]any) { v["job_id"] = id })
			path := filepath.Join(dir, "trial/agent", artifactName)
			mutateJSON(t, path, func(v map[string]any) {
				ext := v["extra"].(map[string]any)["llmspec_model_calls"].(map[string]any)
				ext["execution_generation_id"] = id
				ext["capture_byte_limit"] = mib * 1024 * 1024
				call := ext["calls"].([]any)[0].(map[string]any)
				call["execution_generation_id"] = id
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
