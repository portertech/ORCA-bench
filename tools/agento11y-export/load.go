package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const artifactName = "llmspec-trajectory.atif.json"

type harborConfig struct {
	TrialName string `json:"trial_name"`
	JobID     string `json:"job_id"`
	Agent     struct {
		Model string `json:"model_name"`
	} `json:"agent"`
}
type harborResult struct {
	ID        string       `json:"id"`
	Task      string       `json:"task_name"`
	TrialName string       `json:"trial_name"`
	Config    harborConfig `json:"config"`
	Agent     struct {
		Name    string `json:"name"`
		Version string `json:"version"`
		Model   *struct {
			Name     string `json:"name"`
			Provider string `json:"provider"`
		} `json:"model_info"`
	} `json:"agent_info"`
	Started   string `json:"started_at"`
	Finished  string `json:"finished_at"`
	Execution struct {
		Started  string `json:"started_at"`
		Finished string `json:"finished_at"`
	} `json:"agent_execution"`
	Context struct {
		Input  *int     `json:"n_input_tokens"`
		Output *int     `json:"n_output_tokens"`
		Cache  *int     `json:"n_cache_tokens"`
		Cost   *float64 `json:"cost_usd"`
	} `json:"agent_result"`
	Exception   json.RawMessage   `json:"exception_info"`
	StepResults []json.RawMessage `json:"step_results"`
	Verifier    struct {
		Rewards map[string]float64 `json:"rewards"`
	} `json:"verifier_result"`
}
type loadedTrial struct {
	Result                           harborResult
	Artifact                         artifact
	ArtifactBytes                    []byte
	Hash                             string
	Model                            string
	Job                              string
	Start, End, AgentStart, AgentEnd time.Time
	Reward                           float64
	Rewards                          map[string]float64
	Details                          map[string]any
	Transcript                       string
	Input                            string
}
type loadOptions struct{ Trial, Task, Model, Timezone string }

func readJSON(path string, out any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err = decodeFile(raw, out); err != nil {
		return fmt.Errorf("%s: malformed JSON", path)
	}
	return nil
}
func null(raw json.RawMessage) bool { return len(raw) == 0 || string(raw) == "null" }
func parseTime(value, zone string) (time.Time, error) {
	if t, err := time.Parse(time.RFC3339Nano, value); err == nil {
		return t, nil
	}
	if zone == "" {
		return time.Time{}, errors.New("missing/naive timestamp; supply --harbor-timezone for naive Harbor timestamps")
	}
	loc, err := time.LoadLocation(zone)
	if err != nil {
		return time.Time{}, err
	}
	return time.ParseInLocation("2006-01-02T15:04:05.999999999", value, loc)
}
func digest(raw []byte) string { h := sha256.Sum256(raw); return hex.EncodeToString(h[:]) }
func nonempty(values ...string) (string, error) {
	selected := ""
	for _, v := range values {
		if v == "" {
			continue
		}
		if selected != "" && selected != v {
			return "", errors.New("conflicting provenance")
		}
		selected = v
	}
	if selected == "" {
		return "", errors.New("missing provenance")
	}
	return selected, nil
}
func loadJob(dir string, opts loadOptions) ([]loadedTrial, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	trials := []loadedTrial{}
	ids, execIDs := map[string]bool{}, map[string]bool{}
	jobIdentity := ""
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		base := filepath.Join(dir, entry.Name())
		if _, err = os.Stat(filepath.Join(base, "result.json")); os.IsNotExist(err) {
			continue
		}
		var t loadedTrial
		if err = readJSON(filepath.Join(base, "result.json"), &t.Result); err != nil {
			return nil, err
		}
		r := t.Result
		if opts.Trial != "" && opts.Trial != r.ID && opts.Trial != r.TrialName {
			continue
		}
		if opts.Task != "" && opts.Task != r.Task {
			continue
		}
		var config harborConfig
		if err = readJSON(filepath.Join(base, "config.json"), &config); err != nil && !os.IsNotExist(err) {
			return nil, err
		}
		t.Model, err = nonempty(r.Config.Agent.Model, config.Agent.Model)
		if err != nil {
			return nil, err
		}
		if opts.Model != "" && opts.Model != t.Model {
			continue
		}
		if r.ID == "" || r.Task == "" || r.Agent.Name == "" || r.Agent.Version == "" || ids[r.ID] {
			return nil, errors.New("missing/duplicate trial identity")
		}
		ids[r.ID] = true
		if r.TrialName != "" && config.TrialName != "" && r.TrialName != config.TrialName {
			return nil, errors.New("conflicting trial names")
		}
		if len(r.StepResults) > 0 {
			return nil, errors.New("multi-step trial is unsupported")
		}
		t.Job, err = nonempty(r.Config.JobID, config.JobID)
		if err != nil {
			return nil, errors.New("missing or conflicting Harbor job ID")
		}
		if jobIdentity != "" && jobIdentity != t.Job {
			return nil, errors.New("mixed Harbor job IDs")
		}
		jobIdentity = t.Job
		if r.Agent.Model != nil {
			observed := r.Agent.Model.Name
			if r.Agent.Model.Provider != "" && !strings.HasPrefix(observed, r.Agent.Model.Provider+"/") {
				observed = r.Agent.Model.Provider + "/" + observed
			}
			if observed != t.Model {
				return nil, errors.New("conflicting requested model provenance")
			}
		}
		if t.Start, err = parseTime(r.Started, opts.Timezone); err != nil {
			return nil, err
		}
		if t.End, err = parseTime(r.Finished, opts.Timezone); err != nil {
			return nil, err
		}
		if t.AgentStart, err = parseTime(r.Execution.Started, opts.Timezone); err != nil {
			return nil, err
		}
		if t.AgentEnd, err = parseTime(r.Execution.Finished, opts.Timezone); err != nil {
			return nil, err
		}
		if t.End.Before(t.Start) || t.AgentEnd.Before(t.AgentStart) || t.AgentStart.Before(t.Start) || t.AgentEnd.After(t.End) {
			return nil, errors.New("inconsistent trial timestamps")
		}
		for _, count := range []*int{r.Context.Input, r.Context.Output, r.Context.Cache} {
			if count != nil && *count < 0 {
				return nil, errors.New("negative usage")
			}
		}
		if r.Context.Cost != nil && *r.Context.Cost < 0 {
			return nil, errors.New("negative cost")
		}
		// Reject duplicate artifact basenames anywhere below agent/, not only at the expected path.
		count := 0
		err = filepath.WalkDir(filepath.Join(base, "agent"), func(path string, d os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if d.Name() == artifactName {
				count++
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
		if count != 1 {
			return nil, errors.New("missing/duplicate artifact name")
		}
		t.ArtifactBytes, err = os.ReadFile(filepath.Join(base, "agent", artifactName))
		if err != nil {
			return nil, err
		}
		t.Artifact, err = validateArtifact(t.ArtifactBytes)
		if err != nil {
			return nil, fmt.Errorf("trial %s: invalid model-call artifact: %w", r.ID, err)
		}
		if t.Artifact.Agent.Name != r.Agent.Name || t.Artifact.Agent.Version != r.Agent.Version {
			return nil, errors.New("artifact/Harbor agent identity conflict")
		}
		ext := t.Artifact.Extra.Calls
		if execIDs[ext.ExecutionID] {
			return nil, errors.New("duplicate execution generation ID")
		}
		execIDs[ext.ExecutionID] = true
		for _, c := range ext.Calls {
			if c.RequestedModel != t.Model {
				return nil, errors.New("artifact requested model conflict")
			}
		}
		t.Hash = digest(t.ArtifactBytes)
		var rewards map[string]float64
		if err = readJSON(filepath.Join(base, "verifier", "reward.json"), &rewards); err != nil {
			return nil, err
		}
		var ok bool
		t.Rewards = rewards
		t.Reward, ok = rewards["reward"]
		if !ok {
			return nil, errors.New("missing numeric reward")
		}
		for k, v := range r.Verifier.Rewards {
			if local, ok := rewards[k]; ok && v != local {
				return nil, errors.New("conflicting verifier rewards")
			}
		}
		if err = readJSON(filepath.Join(base, "verifier", "reward-details.json"), &t.Details); err != nil {
			return nil, err
		}
		if t.Details == nil {
			return nil, errors.New("reward details must be an object")
		}
		// Use only human-readable trajectory content for generation I/O.
		var trajectory struct {
			Session string           `json:"session_id"`
			Steps   []map[string]any `json:"steps"`
		}
		if err = readJSON(filepath.Join(base, "agent", "trajectory.json"), &trajectory); err != nil {
			return nil, err
		}
		if trajectory.Session != "" && trajectory.Session != t.Artifact.SessionID {
			return nil, errors.New("conflicting trajectory session IDs")
		}
		var transcript strings.Builder
		for _, step := range trajectory.Steps {
			source, _ := step["source"].(string)
			message, ok := step["message"].(string)
			if !ok {
				b, e := json.Marshal(step["message"])
				if e != nil {
					return nil, e
				}
				message = string(b)
			}
			if t.Input == "" && source == "user" {
				t.Input = message
			}
			if source == "system" {
				source = "environment"
			}
			if source != "user" && source != "agent" && source != "environment" {
				return nil, errors.New("unknown transcript source")
			}
			fmt.Fprintf(&transcript, "[%s]\n%s\n\n", source, message)
		}
		if t.Input == "" {
			return nil, errors.New("missing initial task prompt")
		}
		t.Transcript = transcript.String()
		trials = append(trials, t)
	}
	if len(trials) == 0 {
		return nil, errors.New("no matching completed captured trials")
	}
	sort.Slice(trials, func(i, j int) bool {
		if trials[i].Start.Equal(trials[j].Start) {
			return trials[i].Result.ID < trials[j].Result.ID
		}
		return trials[i].Start.Before(trials[j].Start)
	})
	return trials, nil
}
