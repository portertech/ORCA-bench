package main

import (
	a "github.com/grafana/agento11y/go/agento11y"
	"testing"
)

func TestWriteAPIEndpointEnvironment(t *testing.T) {
	for _, tc := range []struct{ name, api, ingest, want string }{
		{"ingestion default", "", "https://ingest.example.invalid", "https://ingest.example.invalid"},
		{"API takes precedence", "https://api.example.invalid", "https://ingest.example.invalid", "https://api.example.invalid"},
		{"blank API falls back", "  ", " https://ingest.example.invalid ", "https://ingest.example.invalid"},
		{"control proxy is not a write endpoint", "", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENTO11Y_API_ENDPOINT", tc.api)
			t.Setenv("AGENTO11Y_ENDPOINT", tc.ingest)
			t.Setenv("AGENTO11Y_CONTROL_ENDPOINT", "https://grafana.example.invalid/api/plugins/grafana-agento11y-app/resources/eval")
			if got := apiEndpointFromEnv(); got != tc.want {
				t.Fatalf("endpoint = %q, want %q", got, tc.want)
			}
		})
	}
}
func TestIngestActor(t *testing.T) {
	t.Setenv("AGENTO11Y_INGEST_ACTOR", "")
	cfg := a.Config{}
	configureIngestActor(&cfg)
	if cfg.GenerationExport.Headers["X-Sigil-Ingest-Actor"] != "ingest:sdk/go" {
		t.Fatal("missing SDK default actor")
	}
	t.Setenv("AGENTO11Y_INGEST_ACTOR", "synthetic-actor")
	cfg = a.Config{}
	configureIngestActor(&cfg)
	if cfg.GenerationExport.Headers["X-Sigil-Ingest-Actor"] != "synthetic-actor" {
		t.Fatal("configured actor ignored")
	}
	cfg.GenerationExport.Headers = map[string]string{"x-sigil-ingest-actor": "explicit-actor"}
	configureIngestActor(&cfg)
	if len(cfg.GenerationExport.Headers) != 1 || cfg.GenerationExport.Headers["x-sigil-ingest-actor"] != "explicit-actor" {
		t.Fatal("explicit header overwritten")
	}
}
func TestExperimentURLUsesGrafanaRoot(t *testing.T) {
	t.Setenv("AGENTO11Y_GRAFANA_URL", "https://grafana.example.invalid/")
	t.Setenv("AGENTO11Y_EXPERIMENT_URL_TEMPLATE", "")
	t.Setenv("SIGIL_EXPERIMENT_URL_TEMPLATE", "")
	client := a.NewClient(a.Config{GenerationExport: a.GenerationExportConfig{Protocol: a.GenerationExportProtocolNone}, API: a.APIConfig{Endpoint: "https://ingest.example.invalid"}})
	defer client.Shutdown(t.Context())
	want := "https://grafana.example.invalid/a/grafana-agento11y-app/offline-experiments/experiments/synthetic-run"
	if got := experimentURL(client, "synthetic-run"); got != want {
		t.Fatalf("URL = %q, want %q", got, want)
	}
	t.Setenv("AGENTO11Y_EXPERIMENT_URL_TEMPLATE", "https://custom.example.invalid/runs/{run_id}")
	if got := experimentURL(client, "synthetic-run"); got != "https://custom.example.invalid/runs/synthetic-run" {
		t.Fatal("SDK URL template ignored")
	}
}
