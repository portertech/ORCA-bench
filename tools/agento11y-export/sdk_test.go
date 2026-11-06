package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	a "github.com/grafana/agento11y/go/agento11y"
	pb "github.com/grafana/agento11y/go/proto/agento11y/v1"
	"google.golang.org/protobuf/encoding/protojson"
)

// Exercise real SDK serialization/transports, not only the publisher interface.
func TestSDKHTTPPublication(t *testing.T) {
	plan := onePlan(t)
	trial := plan.Trials[0]
	var mu sync.Mutex
	events := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			w.WriteHeader(500)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		send := func(v any) {
			if err := json.NewEncoder(w).Encode(v); err != nil {
				t.Error(err)
			}
		}
		switch {
		case strings.Contains(r.URL.Path, "generations:export"):
			events = append(events, "generation")
			var request pb.ExportGenerationsRequest
			if err := protojson.Unmarshal(raw, &request); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			if len(request.Generations) != 1 || request.Generations[0].Id != trial.Generation.ID || request.Generations[0].ConversationId != trial.Generation.ID {
				t.Error("generation associations lost")
			}
			response := &pb.ExportGenerationsResponse{Results: []*pb.ExportGenerationResult{{GenerationId: trial.Generation.ID, Accepted: true}}}
			body, err := protojson.Marshal(response)
			if err != nil {
				t.Error(err)
			}
			w.Write(body)
		case strings.Contains(r.URL.Path, "scores:export"):
			events = append(events, "score")
			var body struct {
				Scores []map[string]any `json:"scores"`
			}
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Error(err)
			}
			if len(body.Scores) != 1 {
				t.Errorf("score request: %s", raw)
			} else {
				score := body.Scores[0]
				for k, v := range map[string]string{"experiment_id": plan.Experiment.RunID, "trial_id": trial.Trial.TrialID, "test_case_id": trial.Trial.TestCaseID, "generation_id": trial.Generation.ID, "conversation_id": trial.Generation.ID} {
					if score[k] != v {
						t.Errorf("score %s = %v, want %s", k, score[k], v)
					}
				}
			}
			send(map[string]any{"accepted": 1})
		case strings.Contains(r.URL.Path, "artifacts:upload"):
			events = append(events, "artifact")
			if digest(raw) != trial.ArtifactSHA {
				t.Error("SDK changed artifact bytes")
			}
			if r.URL.Query().Get("name") != artifactName || r.URL.Query().Get("kind") != "json" || r.Header.Get("Content-Type") != "application/json" {
				t.Error("artifact metadata lost")
			}
			send(map[string]any{"artifact_id": "synthetic-artifact", "parent_kind": "test_case_trial", "parent_id": trial.Trial.TrialID, "name": artifactName, "kind": "json", "mime": "application/json", "size_bytes": len(raw)})
		case strings.HasSuffix(r.URL.Path, "experiment-runs:upsert"):
			events = append(events, "experiment")
			send(map[string]any{"run_id": plan.Experiment.RunID, "name": plan.Experiment.Name, "status": "running"})
		case strings.Contains(r.URL.Path, "/trials"):
			events = append(events, "trial")
			send(map[string]any{"trial_id": trial.Trial.TrialID, "experiment_id": plan.Experiment.RunID, "test_case_id": trial.Trial.TestCaseID, "status": "running"})
		default:
			events = append(events, "complete")
			send(map[string]any{"run_id": plan.Experiment.RunID, "status": "completed"})
		}
	}))
	defer server.Close()
	client := a.NewClient(a.Config{GenerationExport: a.GenerationExportConfig{Protocol: a.GenerationExportProtocolHTTP, Endpoint: server.URL + "/api/v1/generations:export", Insecure: a.BoolPtr(true), FlushInterval: time.Hour, Auth: a.AuthConfig{Mode: a.ExportAuthModeNone}}, API: a.APIConfig{Endpoint: server.URL}})
	defer client.Shutdown(t.Context())
	if err := publish(t.Context(), client, plan, t.TempDir(), server.URL); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if strings.Join(events, ",") != "experiment,trial,generation,score,artifact,trial,complete" {
		t.Fatal(events)
	}
}
