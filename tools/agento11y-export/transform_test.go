package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	a "github.com/grafana/agento11y/go/agento11y"
)

func TestPublicGoldenValidation(t *testing.T) {
	paths, err := filepath.Glob("../../tests/fixtures/model_calls/*.json")
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			_, err = validateArtifact(raw)
			invalid := filepath.Base(path) == "invalid-response-association.json"
			if (err != nil) != invalid {
				t.Fatalf("validation err=%v", err)
			}
		})
	}
}
func TestStrictJSON(t *testing.T) {
	for _, raw := range []string{`{"x":1,"x":2}`, `{"x":NaN}`, `{} {}`, `{"x":Infinity}`, `{"x":1e9999}`} {
		if _, err := strictJSON([]byte(raw)); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
}
func TestAssociationsAndControlPass(t *testing.T) {
	loaded, err := loadJob(syntheticJob(t), loadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	// Perfect control cases do not have incident-only RCA or hallucination metrics.
	plans, err := plans(loaded, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	p := plans[0]
	trial := p.Trials[0]
	id := loaded[0].Artifact.Extra.Calls.ExecutionID
	if trial.Generation.ID != id || trial.Generation.ConversationID != id || trial.Score.GenerationID != id || trial.Score.ConversationID != id || trial.Trial.ConversationID != id {
		t.Fatal("execution association lost")
	}
	if trial.Score.RunID != p.Experiment.RunID || trial.Score.TrialID != loaded[0].Result.ID || trial.Score.TestCaseID != loaded[0].Result.Task {
		t.Fatal("score association lost")
	}
	if !*trial.Score.Passed || trial.Terminal.Status != "completed" {
		t.Fatal("perfect control did not pass")
	}
}
func TestGroupingAttemptsAndIncomplete(t *testing.T) {
	loaded, err := loadJob(syntheticJob(t), loadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	second := loaded[0]
	second.Result.ID = "second"
	second.Start = second.Start.Add(time.Second)
	second.Reward = 0.4
	loaded = append(loaded, second)
	planned, err := plans(loaded, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	if planned[0].Trials[1].Trial.Attempt != 2 || *planned[0].Trials[1].Score.Passed {
		t.Fatal("incorrect attempt/outcome")
	}
	loaded[1].Model = "other/model"
	planned, err = plans(loaded, testOptions())
	if err != nil || len(planned) != 2 {
		t.Fatal("not grouped per model", err)
	}
	loaded[0].Artifact.Extra.Calls.Rejected = 1
	if _, err = plans(loaded, testOptions()); err == nil {
		t.Fatal("incomplete capture accepted by default")
	}
	opts := testOptions()
	opts.AllowIncomplete = true
	planned, err = plans(loaded, opts)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, p := range planned {
		if p.FinalStatus == a.ExperimentStatusFailed {
			found = true
			if p.Trials[0].Terminal.Status != "failed" {
				t.Fatal("incomplete trial not failed")
			}
		}
	}
	if !found {
		t.Fatal("incomplete experiment not failed")
	}
}
func TestMixedEvaluatorRejected(t *testing.T) {
	loaded, err := loadJob(syntheticJob(t), loadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	loaded[0].Details["evaluator_version"] = "different"
	if _, err = plans(loaded, testOptions()); err == nil {
		t.Fatal("accepted mixed evaluator")
	}
}
func TestExtensionIntegerNotation(t *testing.T) {
	raw, err := os.ReadFile("../../tests/fixtures/model_calls/valid-complete.json")
	if err != nil {
		t.Fatal(err)
	}
	raw = []byte(strings.Replace(string(raw), `"total_tokens": 12`, `"total_tokens": 12.0`, 1))
	if _, err := validateArtifact(raw); err == nil {
		t.Fatal("accepted fractional integer notation")
	}
}

func TestCanonicalBytes(t *testing.T) {
	v, err := strictJSON([]byte("{\"text\":\"é<\u2028\",\"n\":1}"))
	if err != nil {
		t.Fatal(err)
	}
	if canonicalSize(v) != int64(len("{\"text\":\"é<\u2028\",\"n\":1}")) {
		t.Fatal("wrong UTF-8 accounting")
	}
}
func TestSchemaMatchesRepository(t *testing.T) {
	raw, err := os.ReadFile("../../schemas/llmspec-model-calls-v1.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != string(schemaBytes) {
		t.Fatal("embedded schema drift")
	}
	var v any
	if err = json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
}
