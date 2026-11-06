// agento11y-export publishes fresh, captured Harbor trials using the public SDK.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	a "github.com/grafana/agento11y/go/agento11y"
)

func run(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("agento11y-export", flag.ContinueOnError)
	var load loadOptions
	var opts options
	flags.StringVar(&load.Trial, "trial", "", "trial UUID or trial name filter")
	flags.StringVar(&load.Task, "task", "", "exact task filter")
	flags.StringVar(&load.Model, "model", "", "exact requested model filter")
	flags.StringVar(&load.Timezone, "harbor-timezone", "", "explicit IANA zone for naive Harbor timestamps, e.g. UTC")
	flags.StringVar(&opts.RunID, "run-id", "", "run ID override (single experiment only)")
	flags.StringVar(&opts.Name, "name", "", "experiment name override")
	flags.StringVar(&opts.EvaluatorID, "evaluator-id", os.Getenv("ORCA_EVALUATOR_ID"), "required deployment evaluator ID")
	flags.StringVar(&opts.EvaluatorVersion, "evaluator-version", os.Getenv("ORCA_EVALUATOR_VERSION"), "required deployment evaluator version")
	flags.StringVar(&opts.ScoreKey, "score-key", "final", "headline score key")
	flags.BoolVar(&opts.PerModel, "per-model", true, "one experiment per requested model")
	flags.BoolVar(&opts.AllowIncomplete, "allow-incomplete", false, "publish incomplete captures diagnostically with failed status")
	threshold := flags.String("pass-threshold", "", "pass when reward >= threshold (default reward == 1.0)")
	dry := flags.Bool("dry-run", false, "validate and report associations without any network calls")
	snapshot := flags.String("snapshot", "", "write sensitive dry-run payloads to a private JSON file")
	stateDir := flags.String("state-dir", "", "publication receipts (default JOB/.agento11y-export)")
	apiEndpoint := flags.String("api-endpoint", os.Getenv("AGENTO11Y_API_ENDPOINT"), "Agent Observability API endpoint")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 1 {
		return errors.New("usage: agento11y-export [flags] HARBOR_JOB_DIR")
	}
	if *threshold != "" {
		v, err := strconv.ParseFloat(*threshold, 64)
		if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
			return errors.New("invalid pass threshold")
		}
		opts.Threshold = &v
	}
	trials, err := loadJob(flags.Arg(0), load)
	if err != nil {
		return err
	}
	planned, err := plans(trials, opts)
	if err != nil {
		return err
	}
	if *snapshot != "" {
		raw, err := json.MarshalIndent(planned, "", "  ")
		if err != nil {
			return err
		}
		if err = privateWrite(*snapshot, raw); err != nil {
			return err
		}
	}
	for _, p := range planned {
		fmt.Printf("run=%s trials=%d final_status=%s\n", p.Experiment.RunID, len(p.Trials), p.FinalStatus)
		for _, t := range p.Trials {
			fmt.Printf("  trial=%s generation=%s artifact_bytes=%d sha256=%s\n", t.Trial.TrialID, t.Generation.ID, t.ArtifactBytes, t.ArtifactSHA)
		}
	}
	if *dry {
		return nil
	}
	if *stateDir == "" {
		*stateDir = filepath.Join(flags.Arg(0), ".agento11y-export")
	}
	cfg, err := a.ConfigFromEnv()
	if err != nil {
		return errors.New("invalid Agent Observability environment configuration")
	}
	if cfg.GenerationExport.Endpoint == "" || *apiEndpoint == "" {
		return errors.New("publication requires AGENTO11Y_ENDPOINT and --api-endpoint/AGENTO11Y_API_ENDPOINT")
	}
	endpoint, err := url.Parse(*apiEndpoint)
	if err != nil || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" {
		return errors.New("invalid API endpoint")
	}
	if cfg.GenerationExport.Protocol != a.GenerationExportProtocolHTTP && cfg.GenerationExport.Protocol != a.GenerationExportProtocolGRPC {
		return errors.New("publication requires http or grpc generation transport, not instrumentation-only mode")
	}
	cfg.API.Endpoint = *apiEndpoint
	cfg.ContentCapture = a.ContentCaptureModeFull
	client := a.NewClient(cfg)
	defer func() {
		shutdown, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := client.Shutdown(shutdown); err != nil {
			fmt.Fprintln(os.Stderr, "SDK shutdown failed")
		}
	}()
	// Scope receipts to destination and tenant, without storing secrets.
	destination := stableID(*apiEndpoint, cfg.GenerationExport.Endpoint, cfg.GenerationExport.Auth.TenantID)
	for _, p := range planned {
		if err = publish(ctx, client, p, *stateDir, destination); err != nil {
			return err
		}
	}
	return nil
}
func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
