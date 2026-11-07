package main

import (
	"encoding/json"
	"errors"
	"reflect"

	a "github.com/grafana/agento11y/go/agento11y"
)

func validateNative(ext map[string]any, e extension) error {
	if e.Successful != len(e.Calls)+e.Dropped+e.Rejected || (e.Finalized && e.Pending != 0) {
		return errors.New("inconsistent native capture accounting")
	}
	for _, raw := range ext["calls"].([]any) {
		c := raw.(map[string]any)
		messages := c["messages"].([]any)
		if err := nativeHistory(messages, true); err != nil {
			return err
		}
		response := c["response"].(map[string]any)
		if !integerNotation(response["usage"]) || !integerNotation(c["request_options"].(map[string]any)["max_tokens"]) {
			return errors.New("accounting numbers must use integer JSON notation")
		}
		combined := append(append([]any{}, messages...), response["message"])
		if err := nativeHistory(combined, false); err != nil {
			return err
		}
		names := map[string]bool{}
		for _, tool := range c["tools"].([]any) {
			name := tool.(map[string]any)["function"].(map[string]any)["name"].(string)
			if names[name] {
				return errors.New("duplicate native tool names")
			}
			names[name] = true
		}
	}
	return nil
}

func nativeHistory(messages []any, complete bool) error {
	pending, seen := map[string]bool{}, map[string]bool{}
	for _, raw := range messages {
		m := raw.(map[string]any)
		if m["role"] == "tool" {
			id := m["tool_call_id"].(string)
			if !pending[id] {
				return errors.New("orphan or duplicate tool result")
			}
			delete(pending, id)
			continue
		}
		if len(pending) != 0 {
			return errors.New("missing tool result")
		}
		if calls, ok := m["tool_calls"].([]any); ok {
			for _, raw := range calls {
				id := raw.(map[string]any)["id"].(string)
				if seen[id] {
					return errors.New("duplicate tool identity")
				}
				seen[id], pending[id] = true, true
			}
		}
	}
	if complete && len(pending) != 0 {
		return errors.New("unsettled native history")
	}
	return nil
}

// nativeProjection uses the SDK's native parts for the single conversation view.
// Signed/redacted provider fields and raw malformed arguments remain authoritative
// only in the required original-byte artifact; this is an explicitly marked view.
func nativeProjection(g *a.Generation, raw []byte) error {
	value, err := strictJSON(raw)
	if err != nil {
		return err
	}
	ext := value.(map[string]any)["extra"].(map[string]any)["llmspec_model_calls"].(map[string]any)
	calls := ext["calls"].([]any)
	g.Metadata["capture_schema"] = "llmspec.model-calls.v2"
	g.Metadata["native_projection"] = "SDK conversation view; exact provider history in original artifact"
	if len(calls) == 0 {
		return nil
	}
	first, last := calls[0].(map[string]any), calls[len(calls)-1].(map[string]any)
	initial := first["messages"].([]any)
	history := last["messages"].([]any)
	if len(history) < len(initial) || !reflect.DeepEqual(initial, history[:len(initial)]) {
		return errors.New("native history changed initial prompt")
	}
	for _, rawCall := range calls {
		c := rawCall.(map[string]any)
		if !reflect.DeepEqual(c["tools"], first["tools"]) {
			return errors.New("changing native tool definitions unsupported in SDK projection")
		}
	}
	g.Input, g.Output = nil, nil
	for _, rawMessage := range initial {
		m := rawMessage.(map[string]any)
		if m["role"] == "system" {
			g.SystemPrompt, _ = m["content"].(string)
			continue
		}
		g.Input = append(g.Input, projectNativeMessage(m, g.Metadata))
	}
	output := append(append([]any{}, history[len(initial):]...), last["response"].(map[string]any)["message"])
	for _, rawMessage := range output {
		g.Output = append(g.Output, projectNativeMessage(rawMessage.(map[string]any), g.Metadata))
	}
	for _, rawTool := range first["tools"].([]any) {
		tool := rawTool.(map[string]any)["function"].(map[string]any)
		schema, err := json.Marshal(tool["parameters"])
		if err != nil {
			return err
		}
		g.Tools = append(g.Tools, a.ToolDefinition{Name: tool["name"].(string), Description: tool["description"].(string), Type: "function", InputSchema: schema})
	}
	return nil
}

func projectNativeMessage(m map[string]any, metadata map[string]any) a.Message {
	msg := a.Message{Role: a.Role(m["role"].(string)), Parts: []a.Part{}}
	if msg.Role == a.RoleTool {
		text, _ := m["content"].(string)
		msg.Parts = append(msg.Parts, a.ToolResultPart(a.ToolResult{ToolCallID: m["tool_call_id"].(string), Content: text}))
		return msg
	}
	if reasoning, ok := m["reasoning_content"].(string); ok && reasoning != "" {
		msg.Parts = append(msg.Parts, a.Part{Kind: a.PartKindThinking, Thinking: reasoning})
	} else {
		blocks, _ := m["thinking_blocks"].([]any)
		if provider, ok := m["provider_specific_fields"].(map[string]any); ok {
			if nested, ok := provider["thinking_blocks"].([]any); ok {
				blocks = nested
			}
		}
		for _, raw := range blocks {
			block := raw.(map[string]any)
			if text, ok := block["thinking"].(string); ok {
				msg.Parts = append(msg.Parts, a.Part{Kind: a.PartKindThinking, Thinking: text})
			}
		}
	}
	if text, ok := m["content"].(string); ok && text != "" {
		msg.Parts = append(msg.Parts, a.TextPart(text))
	}
	if calls, ok := m["tool_calls"].([]any); ok {
		for _, raw := range calls {
			call := raw.(map[string]any)
			function := call["function"].(map[string]any)
			arguments := []byte(function["arguments"].(string))
			if !json.Valid(arguments) {
				metadata["malformed_tool_arguments_in_original_artifact"] = true
				arguments, _ = json.Marshal(function["arguments"])
			}
			msg.Parts = append(msg.Parts, a.ToolCallPart(a.ToolCall{ID: call["id"].(string), Name: function["name"].(string), InputJSON: arguments}))
		}
	}
	return msg
}
