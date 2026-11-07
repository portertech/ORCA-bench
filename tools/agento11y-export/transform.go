package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	a "github.com/grafana/agento11y/go/agento11y"
)

type options struct {
	RunID, Name, EvaluatorID, EvaluatorVersion, ScoreKey string
	Threshold                                            *float64
	PerModel, AllowIncomplete                            bool
}
type plannedTrial struct {
	Trial         a.UpsertTrialRequest `json:"trial"`
	Generation    a.Generation         `json:"generation"`
	Score         a.ScoreItem          `json:"score"`
	Terminal      a.UpdateTrialRequest `json:"terminal"`
	ArtifactSHA   string               `json:"artifact_sha256"`
	ArtifactBytes int                  `json:"artifact_bytes"`
	Content       []byte               `json:"-"`
}
type experimentPlan struct {
	Experiment  a.CreateExperimentRequest `json:"experiment"`
	Trials      []plannedTrial            `json:"trials"`
	FinalStatus a.ExperimentStatus        `json:"final_status"`
}

func stableID(parts ...string) string { b, _ := json.Marshal(parts); return digest(b)[:32] }
func plans(trials []loadedTrial, opts options) ([]experimentPlan, error) {
	if opts.EvaluatorID == "" || opts.EvaluatorVersion == "" || opts.ScoreKey == "" {
		return nil, errors.New("explicit evaluator ID/version and score key are required")
	}
	groups := map[string][]loadedTrial{}
	for _, t := range trials {
		if t.Artifact.Extra.Calls.incomplete() && !opts.AllowIncomplete {
			return nil, errors.New("incomplete capture requires --allow-incomplete (diagnostic failed publication)")
		}
		key := t.Job + "\x00" + t.Result.Agent.Name + "\x00" + t.Result.Agent.Version
		if opts.PerModel {
			key += "\x00" + t.Model
		}
		groups[key] = append(groups[key], t)
	}
	if opts.RunID != "" && len(groups) != 1 {
		return nil, errors.New("--run-id requires exactly one experiment group")
	}
	keys := make([]string, 0, len(groups))
	for k := range groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	result := []experimentPlan{}
	for _, key := range keys {
		group := groups[key]
		runID := opts.RunID
		if runID == "" {
			runID = "orca-" + stableID(key, opts.EvaluatorID, opts.EvaluatorVersion, opts.ScoreKey)
		}
		name := opts.Name
		if name == "" {
			name = "ORCA " + group[0].Job
		}
		if opts.PerModel {
			name += " / " + group[0].Model
		}
		count := len(group)
		plan := experimentPlan{Experiment: a.CreateExperimentRequest{RunID: runID, Name: name, Source: a.ExperimentSourceExternal, PlannedTrialCount: &count, Metadata: map[string]any{"harbor_job": group[0].Job, "evaluator_id": opts.EvaluatorID, "evaluator_version": opts.EvaluatorVersion}}, FinalStatus: a.ExperimentStatusCompleted}
		attempts := map[string]int{}
		for _, t := range group {
			r := t.Result
			ext := t.Artifact.Extra.Calls
			executionID := ext.ExecutionID
			attemptKey := r.Task + "\x00" + t.Model
			attempts[attemptKey]++
			passed := t.Reward == 1.0
			if opts.Threshold != nil {
				passed = t.Reward >= *opts.Threshold
			}
			outcome := "failed"
			if passed {
				outcome = "passed"
			}
			status := "completed"
			terminalError := ""
			if !null(r.Exception) {
				status = "failed"
				terminalError = "Harbor execution failed"
			}
			if ext.incomplete() {
				status = "failed"
				terminalError = "Incomplete model-call capture"
				plan.FinalStatus = a.ExperimentStatusFailed
			}
			metadata := map[string]any{"harbor_job": t.Job, "task": r.Task, "requested_model": t.Model, "benchmark_passed": passed, "capture_complete": !ext.incomplete(), "artifact_sha256": t.Hash, "truncated": ext.Truncated, "dropped_calls": ext.Dropped, "rejected_calls": ext.Rejected, "cost_usd": r.Context.Cost, "duration_ms": t.AgentEnd.Sub(t.AgentStart).Milliseconds()}
			for _, k := range []string{"rca_accuracy", "hallucinate_any"} {
				if value, ok := t.Rewards[k]; ok {
					metadata[k] = value
				}
			}
			if version, ok := t.Details["evaluator_version"].(string); ok && version != opts.EvaluatorVersion {
				return nil, errors.New("mixed evaluator versions require separate experiments")
			}
			for _, k := range []string{"mode", "model", "reasoning_effort"} {
				if value, ok := t.Details[k].(string); ok {
					metadata["verifier_"+k] = value
				}
			}
			model := a.ModelRef{Name: t.Model, Provider: "unknown"}
			if provider, name, ok := strings.Cut(t.Model, "/"); ok {
				model = a.ModelRef{Provider: provider, Name: name}
			}
			usage := a.TokenUsage{}
			if r.Context.Input != nil {
				usage.InputTokens = int64(*r.Context.Input)
			}
			if r.Context.Output != nil {
				usage.OutputTokens = int64(*r.Context.Output)
			}
			if r.Context.Cache != nil {
				usage.CacheReadInputTokens = int64(*r.Context.Cache)
			}
			generation := a.Generation{ID: executionID, ConversationID: executionID, AgentName: r.Agent.Name, AgentVersion: r.Agent.Version, Mode: a.GenerationModeSync, Model: model, Input: []a.Message{a.UserTextMessage(t.Input)}, Output: []a.Message{a.AssistantTextMessage(t.Transcript)}, StartedAt: t.AgentStart, CompletedAt: t.AgentEnd, Usage: usage, Tags: map[string]string{"harbor_job": t.Job, "trial": r.ID, "task": r.Task, "model": t.Model, "producer_version": t.Artifact.Agent.Version, "outcome": outcome}, Metadata: metadata}
			if ext.Version == "llmspec.model-calls.v2" {
				if err := nativeProjection(&generation, t.ArtifactBytes); err != nil {
					return nil, err
				}
			}
			duration := int(t.AgentEnd.Sub(t.AgentStart).Milliseconds())
			trial := plannedTrial{Trial: a.UpsertTrialRequest{TrialID: r.ID, TestCaseID: r.Task, Attempt: attempts[attemptKey], Status: "running", ConversationID: executionID, Metadata: metadata}, Generation: generation, Score: a.ScoreItem{ScoreID: stableID(runID, r.ID, executionID, opts.EvaluatorID, opts.EvaluatorVersion, opts.ScoreKey), GenerationID: executionID, ConversationID: executionID, RunID: runID, TrialID: r.ID, TestCaseID: r.Task, EvaluatorID: opts.EvaluatorID, EvaluatorVersion: opts.EvaluatorVersion, ScoreKey: opts.ScoreKey, Value: a.NumberScoreValue(t.Reward), Passed: &passed, Explanation: fmt.Sprintf("ORCA verifier reward %.6g; benchmark_passed=%t", t.Reward, passed), Metadata: metadata, CreatedAt: &t.End}, Terminal: a.UpdateTrialRequest{Status: status, Error: terminalError, ConversationID: executionID, Cost: r.Context.Cost, InputTokens: r.Context.Input, OutputTokens: r.Context.Output, DurationMillis: &duration}, ArtifactSHA: t.Hash, ArtifactBytes: len(t.ArtifactBytes), Content: t.ArtifactBytes}
			plan.Trials = append(plan.Trials, trial)
		}
		result = append(result, plan)
	}
	return result, nil
}
