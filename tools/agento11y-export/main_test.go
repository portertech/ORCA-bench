package main

import "testing"

func TestAPIEndpointEnvironmentAliases(t *testing.T) {
	for _, tc := range []struct{ name, api, control, want string }{
		{"control alias", "", "https://control.example.invalid", "https://control.example.invalid"},
		{"API takes precedence", "https://api.example.invalid", "https://control.example.invalid", "https://api.example.invalid"},
		{"blank API falls back", "  ", " https://control.example.invalid ", "https://control.example.invalid"},
		{"missing", "", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENTO11Y_API_ENDPOINT", tc.api)
			t.Setenv("AGENTO11Y_CONTROL_ENDPOINT", tc.control)
			if got := apiEndpointFromEnv(); got != tc.want {
				t.Fatalf("endpoint = %q, want %q", got, tc.want)
			}
		})
	}
}
