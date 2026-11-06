package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	a "github.com/grafana/agento11y/go/agento11y"
)

type publisher interface {
	CreateExperiment(context.Context, a.CreateExperimentRequest) (*a.Experiment, error)
	UpsertTrial(context.Context, string, a.UpsertTrialRequest) (*a.TestCaseTrial, error)
	ExportGeneration(context.Context, a.GenerationStart, a.Generation) error
	Flush(context.Context) error
	ExportScores(context.Context, []a.ScoreItem) (*a.ExportScoresResponse, error)
	UploadTrialArtifact(context.Context, string, string, a.TrialArtifactUpload) (*a.TrialArtifact, error)
	UpdateTrial(context.Context, string, string, a.UpdateTrialRequest) (*a.TestCaseTrial, error)
	CompleteExperiment(context.Context, string, a.ExperimentStatus, a.CompleteExperimentOptions) (*a.Experiment, error)
}

type receipt struct {
	Fingerprint string            `json:"fingerprint"`
	Uploads     map[string]string `json:"uploads"` // pending or confirmed artifact hash
	Complete    bool              `json:"complete"`
}

func privateWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".receipt-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(name, path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
func saveReceipt(path string, r receipt) error {
	raw, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	return privateWrite(path, raw)
}

// Only explicit asynchronous generation visibility races are retried here.
// The SDK owns its own bounded network/5xx retry policy.
func generationRace(message string) bool {
	m := strings.ToLower(message)
	return strings.Contains(m, "generation_not_found") || strings.Contains(m, "generation not found")
}
func exportScore(ctx context.Context, p publisher, score a.ScoreItem) error {
	for attempt := 0; attempt < 6; attempt++ {
		response, err := p.ExportScores(ctx, []a.ScoreItem{score})
		retry := false
		if err != nil {
			retry = generationRace(err.Error())
		} else if response == nil {
			return errors.New("missing score export response")
		} else {
			rejected := response.Rejected()
			if len(rejected) == 0 && response.RejectedCount == 0 && response.AcceptedCount()+response.DuplicateCount() == 1 {
				return nil
			}
			retry = len(rejected) > 0
			for _, r := range rejected {
				retry = retry && generationRace(r.Error)
			}
			err = errors.New("score rejected")
		}
		if !retry || attempt == 5 {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(1<<attempt) * 100 * time.Millisecond):
		}
	}
	return errors.New("score export exhausted")
}

func publish(ctx context.Context, p publisher, plan experimentPlan, stateDir, destination string) (err error) {
	// Lock and receipt are local safety rails. Pending uploads are deliberately NOT
	// retried after an ambiguous crash; reconcile server state before continuing.
	runID := plan.Experiment.RunID
	path := filepath.Join(stateDir, stableID(destination, runID)+".json")
	if err = os.MkdirAll(stateDir, 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return errors.New("publication locked; reconcile a prior interrupted run before removing its lock")
	}
	lock.Close()
	defer os.Remove(path + ".lock")
	snapshot, err := json.Marshal(plan)
	if err != nil {
		return err
	}
	r := receipt{Fingerprint: digest(snapshot), Uploads: map[string]string{}}
	raw, readErr := os.ReadFile(path)
	if readErr == nil {
		if err = decodeFile(raw, &r); err != nil {
			return err
		}
		if r.Fingerprint != digest(snapshot) {
			return errors.New("run receipt conflicts with changed inputs/options; use a new run ID")
		}
		if r.Complete {
			return nil
		}
		for _, v := range r.Uploads {
			if v == "pending" {
				return errors.New("ambiguous pending artifact upload; reconcile server artifact before retry")
			}
		}
	} else if !os.IsNotExist(readErr) {
		return readErr
	}
	if err = saveReceipt(path, r); err != nil {
		return err
	}
	if _, err = p.CreateExperiment(ctx, plan.Experiment); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
			defer cancel()
			_, markErr := p.CompleteExperiment(cleanup, runID, a.ExperimentStatusFailed, a.CompleteExperimentOptions{Error: "Required publication operation failed; consult local receipt"})
			if markErr != nil {
				err = errors.Join(err, errors.New("also failed to mark experiment failed"))
			}
		}
	}()
	for _, trial := range plan.Trials {
		if _, err = p.UpsertTrial(ctx, runID, trial.Trial); err != nil {
			return err
		}
	}
	for _, trial := range plan.Trials {
		if err = p.ExportGeneration(ctx, a.GenerationStart{ID: trial.Generation.ID, ConversationID: trial.Generation.ConversationID, StartedAt: trial.Generation.StartedAt}, trial.Generation); err != nil {
			return err
		}
	}
	if err = p.Flush(ctx); err != nil {
		return err
	}
	for _, trial := range plan.Trials {
		if err = exportScore(ctx, p, trial.Score); err != nil {
			return err
		}
	}
	for _, trial := range plan.Trials {
		id := trial.Trial.TrialID
		if hash, ok := r.Uploads[id]; ok {
			if hash != trial.ArtifactSHA {
				return errors.New("artifact receipt hash mismatch")
			}
			continue
		}
		r.Uploads[id] = "pending"
		if err = saveReceipt(path, r); err != nil {
			return err
		}
		uploaded, uploadErr := p.UploadTrialArtifact(ctx, runID, id, a.TrialArtifactUpload{Name: artifactName, Kind: "json", MIME: "application/json", Content: trial.Content})
		if uploadErr != nil {
			return fmt.Errorf("mandatory trial artifact upload failed: %w", uploadErr)
		}
		if uploaded == nil || uploaded.ArtifactID == "" || uploaded.Name != artifactName || uploaded.Kind != "json" || uploaded.MIME != "application/json" || uploaded.ParentKind != "test_case_trial" || uploaded.ParentID != id {
			return errors.New("artifact response has invalid associations")
		}
		if uploaded.SizeBytes != nil && *uploaded.SizeBytes != int64(len(trial.Content)) {
			return errors.New("artifact response size mismatch")
		}
		r.Uploads[id] = trial.ArtifactSHA
		if err = saveReceipt(path, r); err != nil {
			return err
		}
	}
	for _, trial := range plan.Trials {
		if _, err = p.UpdateTrial(ctx, runID, trial.Trial.TrialID, trial.Terminal); err != nil {
			return err
		}
	}
	count := len(plan.Trials)
	if _, err = p.CompleteExperiment(ctx, runID, plan.FinalStatus, a.CompleteExperimentOptions{ScoreCount: &count}); err != nil {
		return err
	}
	r.Complete = true
	return saveReceipt(path, r)
}
