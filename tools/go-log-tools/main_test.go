package main

import "testing"

func TestAnalyzeDeviceLoginFailed(t *testing.T) {
	text := `2026-07-01T10:00:01+08:00 info gateway route/v1/device login request request_id=req-login-001 uri=/v1/device/login uuid=device-001 ip=10.0.0.8
2026-07-01T10:00:02+08:00 error gateway mysql update error request_id=req-login-001 uuid=device-001 error="record not found"
2026-07-01T10:00:03+08:00 error gateway response/base.go:32 request_id=req-login-001 error="UuidInvalid"`

	result := analyze(text, "gateway.log")

	if result.Service != "gateway" {
		t.Fatalf("service = %q, want gateway", result.Service)
	}
	if result.RiskLevel != "high" {
		t.Fatalf("risk_level = %q, want high", result.RiskLevel)
	}
	if result.ErrorCount != 2 {
		t.Fatalf("error_count = %d, want 2", result.ErrorCount)
	}
	if result.Incidents["device_login_failed"] == 0 {
		t.Fatalf("device_login_failed incident missing: %#v", result.Incidents)
	}
	if result.Incidents["database_connection_failed"] != 0 {
		t.Fatalf("unexpected database incident: %#v", result.Incidents)
	}
	if len(result.MessageIDs) != 1 || result.MessageIDs[0] != "req-login-001" {
		t.Fatalf("message_ids = %#v, want [req-login-001]", result.MessageIDs)
	}
}

func TestRedact(t *testing.T) {
	line := `ip=192.168.1.10 phone=13800138000 token=abc123 password="secret"`
	got := redact(line)
	for _, raw := range []string{"192.168.1.10", "13800138000", "abc123", "secret"} {
		if containsAny(got, []string{raw}) {
			t.Fatalf("redacted line still contains %q: %s", raw, got)
		}
	}
}
