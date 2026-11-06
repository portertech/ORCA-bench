package main

import (
	"context"
	"errors"
	"reflect"
	"testing"

	a "github.com/grafana/agento11y/go/agento11y"
)

type fakePublisher struct {
	events        []string
	fail          string
	uploaded      []byte
	scoreAttempts int
	race          bool
}

func (f *fakePublisher) event(name string) error {
	f.events = append(f.events, name)
	if f.fail == name {
		return errors.New("synthetic failure")
	}
	return nil
}
func (f *fakePublisher) CreateExperiment(context.Context, a.CreateExperimentRequest) (*a.Experiment, error) {
	return &a.Experiment{}, f.event("experiment")
}
func (f *fakePublisher) UpsertTrial(context.Context, string, a.UpsertTrialRequest) (*a.TestCaseTrial, error) {
	return &a.TestCaseTrial{}, f.event("trial-running")
}
func (f *fakePublisher) ExportGeneration(context.Context, a.GenerationStart, a.Generation) error {
	return f.event("generation")
}
func (f *fakePublisher) Flush(context.Context) error { return f.event("flush") }
func (f *fakePublisher) ExportScores(_ context.Context, scores []a.ScoreItem) (*a.ExportScoresResponse, error) {
	f.scoreAttempts++
	if err := f.event("score"); err != nil {
		return nil, err
	}
	if f.race && f.scoreAttempts == 1 {
		return &a.ExportScoresResponse{Results: []a.ExportScoreResult{{ScoreID: scores[0].ScoreID, Error: "generation_not_found"}}}, nil
	}
	return &a.ExportScoresResponse{Accepted: 1}, nil
}
func (f *fakePublisher) UploadTrialArtifact(_ context.Context, _ string, id string, upload a.TrialArtifactUpload) (*a.TrialArtifact, error) {
	if err := f.event("artifact"); err != nil {
		return nil, err
	}
	f.uploaded = upload.Content
	size := int64(len(upload.Content))
	return &a.TrialArtifact{ArtifactID: "synthetic-artifact", ParentKind: "test_case_trial", ParentID: id, Name: upload.Name, Kind: upload.Kind, MIME: upload.MIME, SizeBytes: &size}, nil
}
func (f *fakePublisher) UpdateTrial(context.Context, string, string, a.UpdateTrialRequest) (*a.TestCaseTrial, error) {
	return &a.TestCaseTrial{}, f.event("trial-terminal")
}
func (f *fakePublisher) CompleteExperiment(_ context.Context, _ string, status a.ExperimentStatus, _ a.CompleteExperimentOptions) (*a.Experiment, error) {
	return &a.Experiment{}, f.event("experiment-" + string(status))
}
func onePlan(t *testing.T) experimentPlan {
	t.Helper()
	trials, err := loadJob(syntheticJob(t), loadOptions{})
	if err != nil {
		t.Fatal(err)
	}
	planned, err := plans(trials, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	return planned[0]
}
func TestPublishOrderIntegrityAndIdempotency(t *testing.T) {
	plan := onePlan(t)
	f := &fakePublisher{}
	state := t.TempDir()
	if err := publish(t.Context(), f, plan, state, "synthetic-destination"); err != nil {
		t.Fatal(err)
	}
	want := []string{"experiment", "trial-running", "generation", "flush", "score", "artifact", "trial-terminal", "experiment-completed"}
	if !reflect.DeepEqual(f.events, want) {
		t.Fatal(f.events)
	}
	if digest(f.uploaded) != plan.Trials[0].ArtifactSHA {
		t.Fatal("artifact bytes changed")
	}
	if err := publish(t.Context(), f, plan, state, "synthetic-destination"); err != nil {
		t.Fatal(err)
	}
	if len(f.events) != len(want) {
		t.Fatal("republication duplicated operations")
	}
	plan.Experiment.Name = "changed"
	if err := publish(t.Context(), f, plan, state, "synthetic-destination"); err == nil {
		t.Fatal("changed run accepted")
	}
}
func TestPublicationFailuresPreventCompletion(t *testing.T) {
	for _, failure := range []string{"trial-running", "generation", "flush", "score", "artifact", "trial-terminal"} {
		t.Run(failure, func(t *testing.T) {
			f := &fakePublisher{fail: failure}
			if err := publish(t.Context(), f, onePlan(t), t.TempDir(), "synthetic"); err == nil {
				t.Fatal("failure swallowed")
			}
			if f.events[len(f.events)-1] != "experiment-failed" {
				t.Fatal("experiment not failed", f.events)
			}
			for _, event := range f.events {
				if event == "experiment-completed" {
					t.Fatal("completed after failure")
				}
			}
		})
	}
}
func TestAmbiguousUploadNotRetried(t *testing.T) {
	plan := onePlan(t)
	state := t.TempDir()
	f := &fakePublisher{fail: "artifact"}
	if err := publish(t.Context(), f, plan, state, "synthetic"); err == nil {
		t.Fatal("failure swallowed")
	}
	f.fail = ""
	count := len(f.events)
	if err := publish(t.Context(), f, plan, state, "synthetic"); err == nil {
		t.Fatal("ambiguous upload retried")
	}
	if len(f.events) != count {
		t.Fatal("network operations after ambiguous upload")
	}
}
func TestOnlyGenerationVisibilityRaceRetried(t *testing.T) {
	f := &fakePublisher{race: true}
	if err := exportScore(t.Context(), f, a.ScoreItem{ScoreID: "synthetic"}); err != nil {
		t.Fatal(err)
	}
	if f.scoreAttempts != 2 {
		t.Fatal("race not retried")
	}
	f = &fakePublisher{fail: "score"}
	if err := exportScore(t.Context(), f, a.ScoreItem{}); err == nil || f.scoreAttempts != 1 {
		t.Fatal("retried unrelated error")
	}
}
