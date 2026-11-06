package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeJSON(t *testing.T, path string, v any) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
}
func syntheticJob(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	base := filepath.Join(dir, "trial")
	raw, err := os.ReadFile("../../tests/fixtures/model_calls/valid-complete.json")
	if err != nil {
		t.Fatal(err)
	}
	var artifact map[string]any
	if err = json.Unmarshal(raw, &artifact); err != nil {
		t.Fatal(err)
	}
	artifact["steps"] = []any{map[string]any{"step_id": 1, "source": "user", "message": "synthetic task"}}
	writeJSON(t, filepath.Join(base, "agent", artifactName), artifact)
	config := map[string]any{"trial_name": "synthetic-trial", "job_id": "synthetic-job", "agent": map[string]any{"model_name": "synthetic/requested"}}
	result := map[string]any{"id": "synthetic-trial-id", "task_name": "synthetic-task", "trial_name": "synthetic-trial", "config": config, "agent_info": map[string]any{"name": "terminus-2", "version": "2.0.0+orca-model-calls.1", "model_info": map[string]any{"provider": "synthetic", "name": "requested"}}, "agent_result": map[string]any{"n_input_tokens": 10, "n_output_tokens": 2, "cost_usd": 0.001}, "started_at": "2024-01-01T00:00:00Z", "finished_at": "2024-01-01T00:01:00Z", "agent_execution": map[string]any{"started_at": "2024-01-01T00:00:10Z", "finished_at": "2024-01-01T00:00:50Z"}}
	writeJSON(t, filepath.Join(base, "result.json"), result)
	writeJSON(t, filepath.Join(base, "config.json"), config)
	writeJSON(t, filepath.Join(base, "agent", "trajectory.json"), map[string]any{"session_id": "synthetic-session", "steps": []any{map[string]any{"source": "user", "message": "synthetic task"}, map[string]any{"source": "agent", "message": "synthetic answer"}}})
	writeJSON(t, filepath.Join(base, "verifier", "reward.json"), map[string]any{"reward": 1.0})
	writeJSON(t, filepath.Join(base, "verifier", "reward-details.json"), map[string]any{"mode": "no_incident_llm_judge", "model": "synthetic-judge", "reasoning_effort": "medium"})
	return dir
}
func mutateJSON(t *testing.T, path string, fn func(map[string]any)) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err = json.Unmarshal(b, &v); err != nil {
		t.Fatal(err)
	}
	fn(v)
	writeJSON(t, path, v)
}
func testOptions() options {
	return options{EvaluatorID: "synthetic-verifier", EvaluatorVersion: "synthetic-v1", ScoreKey: "final", PerModel: true}
}
func TestLoadAndDryRun(t *testing.T) {
	dir := syntheticJob(t)
	trials, err := loadJob(dir, loadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(trials) != 1 || trials[0].Reward != 1 || !strings.Contains(trials[0].Transcript, "[agent]") {
		t.Fatal("incorrect loaded trial")
	}
	// Dry-run needs no credentials or network. Snapshot permissions protect prompts.
	snapshot := filepath.Join(t.TempDir(), "snapshot.json")
	if err = run(t.Context(), []string{"--dry-run", "--snapshot", snapshot, "--evaluator-id", "synthetic", "--evaluator-version", "v1", dir}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatal("snapshot is not private")
	}
}
func TestFailClosedLoading(t *testing.T) {
	cases := map[string]func(*testing.T, string){
		"missing completed time": func(t *testing.T, d string) {
			mutateJSON(t, filepath.Join(d, "trial/result.json"), func(v map[string]any) { delete(v, "finished_at") })
		},
		"conflicting model": func(t *testing.T, d string) {
			mutateJSON(t, filepath.Join(d, "trial/config.json"), func(v map[string]any) { v["agent"] = map[string]any{"model_name": "other"} })
		},
		"non numeric reward": func(t *testing.T, d string) {
			writeJSON(t, filepath.Join(d, "trial/verifier/reward.json"), map[string]any{"reward": "1"})
		},
		"invalid artifact": func(t *testing.T, d string) {
			mutateJSON(t, filepath.Join(d, "trial/agent", artifactName), func(v map[string]any) { v["unknown"] = true })
		},
		"duplicate artifact": func(t *testing.T, d string) {
			writeJSON(t, filepath.Join(d, "trial/agent/nested", artifactName), map[string]any{})
		},
		"conflicting session": func(t *testing.T, d string) {
			mutateJSON(t, filepath.Join(d, "trial/agent/trajectory.json"), func(v map[string]any) { v["session_id"] = "other" })
		},
		"duplicate trial": func(t *testing.T, d string) {
			err := os.CopyFS(filepath.Join(d, "duplicate"), os.DirFS(filepath.Join(d, "trial")))
			if err != nil {
				t.Fatal(err)
			}
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			dir := syntheticJob(t)
			mutate(t, dir)
			if _, err := loadJob(dir, loadOptions{}); err == nil {
				t.Fatal("accepted invalid trial")
			}
		})
	}
}
func TestNaiveTimestampRequiresExplicitZone(t *testing.T) {
	if _, err := parseTime("2024-01-01T00:00:00", ""); err == nil {
		t.Fatal("guessed timezone")
	}
	if _, err := parseTime("2024-01-01T00:00:00", "UTC"); err != nil {
		t.Fatal(err)
	}
}
