package main

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

//go:embed schema.json
var schemaBytes []byte

//go:embed schema-v2.json
var schemaV2Bytes []byte

// strictJSON also rejects duplicate keys, including inside inert metadata.
func strictJSON(raw []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var value func() (any, error)
	value = func() (any, error) {
		token, err := d.Token()
		if err != nil {
			return nil, err
		}
		delim, ok := token.(json.Delim)
		if !ok {
			if n, ok := token.(json.Number); ok && strings.ContainsAny(string(n), ".eE") {
				if _, err := strconv.ParseFloat(string(n), 64); err != nil {
					return nil, errors.New("non-finite JSON number")
				}
			}
			return token, nil
		}
		switch delim {
		case '{':
			m := map[string]any{}
			for d.More() {
				k, err := d.Token()
				if err != nil {
					return nil, err
				}
				key, ok := k.(string)
				if !ok {
					return nil, errors.New("invalid key")
				}
				if _, exists := m[key]; exists {
					return nil, errors.New("duplicate JSON key")
				}
				v, err := value()
				if err != nil {
					return nil, err
				}
				m[key] = v
			}
			_, err = d.Token()
			return m, err
		case '[':
			a := []any{}
			for d.More() {
				v, err := value()
				if err != nil {
					return nil, err
				}
				a = append(a, v)
			}
			_, err = d.Token()
			return a, err
		default:
			return nil, errors.New("unexpected delimiter")
		}
	}
	v, err := value()
	if err != nil {
		return nil, err
	}
	if _, err = d.Token(); err != io.EOF {
		return nil, errors.New("trailing JSON data")
	}
	return v, nil
}

type artifact struct {
	SessionID string `json:"session_id"`
	Agent     struct {
		Name    string `json:"name"`
		Version string `json:"version"`
	} `json:"agent"`
	Steps []map[string]any `json:"steps"`
	Extra struct {
		Calls extension `json:"llmspec_model_calls"`
	} `json:"extra"`
}
type extension struct {
	Version     string      `json:"schema_version"`
	Finalized   bool        `json:"finalized"`
	Pending     int         `json:"pending_requests"`
	Uncertain   int         `json:"uncertain_requests"`
	Successful  int         `json:"successful_responses"`
	ExecutionID string      `json:"execution_generation_id"`
	ByteLimit   int64       `json:"capture_byte_limit"`
	Truncated   bool        `json:"truncated"`
	Dropped     int         `json:"dropped_calls"`
	Rejected    int         `json:"rejected_calls"`
	Calls       []modelCall `json:"calls"`
}
type modelCall struct {
	RequestID      string `json:"request_id"`
	ExecutionID    string `json:"execution_generation_id"`
	Ordinal        int    `json:"ordinal"`
	Step           int    `json:"step_number"`
	RequestedModel string `json:"requested_model"`
	Provider       string `json:"runtime_provider"`
	Model          string `json:"runtime_model"`
	Response       struct {
		ID        string `json:"id"`
		RequestID string `json:"request_id"`
		Step      int    `json:"step_number"`
		Model     string `json:"model"`
		Timestamp string `json:"timestamp"`
	} `json:"response"`
}

func (e extension) incomplete() bool {
	return e.Truncated || e.Dropped > 0 || e.Rejected > 0 ||
		(e.Version == "llmspec.model-calls.v2" && (!e.Finalized || e.Pending > 0 || e.Uncertain > 0))
}

// canonicalSize matches Python ensure_ascii=False compact JSON string escaping.
// Numbers in capture accounting are integers; inert numeric metadata uses its JSON spelling.
func canonicalSize(v any) int64 {
	switch v := v.(type) {
	case string:
		size := int64(2)
		for _, r := range v {
			switch r {
			case '\\', '"', '\b', '\f', '\n', '\r', '\t':
				size += 2
			default:
				if r < 32 {
					size += 6
				} else {
					size += int64(len(string(r)))
				}
			}
		}
		return size
	case map[string]any:
		size := int64(2)
		for k, x := range v {
			size += canonicalSize(k) + 1 + canonicalSize(x) + 1
		}
		if len(v) > 0 {
			size--
		}
		return size
	case []any:
		size := int64(2)
		for _, x := range v {
			size += canonicalSize(x) + 1
		}
		if len(v) > 0 {
			size--
		}
		return size
	default:
		b, _ := json.Marshal(v)
		return int64(len(b))
	}
}

func validateArtifact(raw []byte) (artifact, error) {
	var a artifact
	v, err := strictJSON(raw)
	if err != nil {
		return a, err
	}
	selectedSchema := schemaBytes
	if root, ok := v.(map[string]any); ok {
		if extra, ok := root["extra"].(map[string]any); ok {
			if ext, ok := extra["llmspec_model_calls"].(map[string]any); ok && ext["schema_version"] == "llmspec.model-calls.v2" {
				selectedSchema = schemaV2Bytes
			}
		}
	}
	schema, err := jsonschema.UnmarshalJSON(bytes.NewReader(selectedSchema))
	if err != nil {
		return a, err
	}
	compiler := jsonschema.NewCompiler()
	compiler.AssertFormat()
	if err = compiler.AddResource("schema.json", schema); err != nil {
		return a, err
	}
	compiled, err := compiler.Compile("schema.json")
	if err != nil {
		return a, err
	}
	if err = compiled.Validate(v); err != nil {
		return a, errors.New("artifact violates public JSON schema")
	}
	if err = json.Unmarshal(raw, &a); err != nil {
		return a, err
	}
	e := a.Extra.Calls
	requests, responses := map[string]bool{}, map[string]bool{}
	previous := 0
	for _, c := range e.Calls {
		if requests[c.RequestID] || responses[c.Response.ID] || c.Ordinal <= previous {
			return a, errors.New("duplicate or unordered call identity")
		}
		requests[c.RequestID] = true
		responses[c.Response.ID] = true
		previous = c.Ordinal
		if c.Step != c.Ordinal || c.ExecutionID != e.ExecutionID || c.Response.RequestID != c.RequestID || c.Response.Step != c.Step || c.Response.Model != c.Model {
			return a, errors.New("mismatched response association")
		}
		if _, err = time.Parse(time.RFC3339Nano, c.Response.Timestamp); err != nil {
			return a, errors.New("invalid response timestamp")
		}
	}
	if previous > len(e.Calls)+e.Dropped+e.Rejected {
		return a, errors.New("unaccounted ordinal gap")
	}
	if e.Dropped > 0 && !e.Truncated {
		return a, errors.New("dropped calls without truncation")
	}
	root := v.(map[string]any)
	ext := root["extra"].(map[string]any)["llmspec_model_calls"]
	if e.Version == "llmspec.model-calls.v2" {
		if err := validateNative(ext.(map[string]any), e); err != nil {
			return a, err
		}
	} else if !integerNotation(ext) {
		return a, errors.New("extension numbers must use integer JSON notation")
	}
	if canonicalSize(ext) > e.ByteLimit && !e.Truncated {
		return a, errors.New("extension exceeds capture bound")
	}
	previous = 0
	for _, rawStep := range root["steps"].([]any) {
		step := rawStep.(map[string]any)
		n, err := strconv.Atoi(fmt.Sprint(step["step_id"]))
		if err != nil || n <= previous {
			return a, errors.New("unordered transcript IDs")
		}
		previous = n
	}
	return a, nil
}

func integerNotation(v any) bool {
	switch v := v.(type) {
	case json.Number:
		return !strings.ContainsAny(string(v), ".eE")
	case map[string]any:
		for _, child := range v {
			if !integerNotation(child) {
				return false
			}
		}
	case []any:
		for _, child := range v {
			if !integerNotation(child) {
				return false
			}
		}
	}
	return true
}

func decodeFile(raw []byte, out any) error {
	if _, err := strictJSON(raw); err != nil {
		return err
	}
	return json.Unmarshal(raw, out)
}
