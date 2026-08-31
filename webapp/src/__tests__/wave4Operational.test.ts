import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AUTH_FAILURE,
  BACKEND_NOT_READY,
  MALFORMED_DIAGNOSTIC_WIRE,
  authFailureDiagnostic,
  fromBackendDiagnostic,
  malformedBackendDiagnostic,
  parseBackendDiagnostic,
} from "../lib/operationalDiagnostic";
import { clearDiagnostic, getActiveDiagnostic, publishDiagnostic } from "../lib/operationalDiagnosticBus";
import { executeDiagnosticRecovery } from "../lib/operationalRecovery";

afterEach(() => {
  clearDiagnostic();
});

describe("fromBackendDiagnostic", () => {
  it("parses GET /api/diagnostics wire payloads", () => {
    const diag = fromBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      code: BACKEND_NOT_READY,
      summary: "durable state failed",
      detail: "store error",
      severity: "error",
      retryable: true,
      recovery: { kind: "retry", label: "Retry" },
      createdAt: 1,
      correlation_id: "diag-wire-1",
    });
    expect(diag?.scope).toBe("backend");
    expect(diag?.recovery).toEqual({ kind: "retry", label: "Retry" });
    expect(diag?.correlationId).toBe("diag-wire-1");
  });

  it("returns null for incomplete wire payloads", () => {
    expect(fromBackendDiagnostic({ summary: "only summary" })).toBeNull();
  });

  it("treats string false as not retryable", () => {
    const diag = fromBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      code: BACKEND_NOT_READY,
      summary: "durable state failed",
      severity: "error",
      retryable: "false",
    });
    expect(diag?.retryable).toBe(false);
  });

  it("rejects unknown scope and severity", () => {
    expect(fromBackendDiagnostic({
      scope: "not-a-scope" as never,
      operation: "doctor",
      summary: "x",
      severity: "error",
    })).toBeNull();
    expect(fromBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      summary: "x",
      severity: "fatal" as never,
    })).toBeNull();
  });
});

describe("parseBackendDiagnostic", () => {
  it("distinguishes absent from malformed", () => {
    expect(parseBackendDiagnostic(null)).toEqual({ status: "absent" });
    expect(parseBackendDiagnostic(undefined)).toEqual({ status: "absent" });
    expect(parseBackendDiagnostic({})).toEqual({ status: "absent" });
    const incomplete = parseBackendDiagnostic({ summary: "only summary" });
    expect(incomplete.status).toBe("invalid");
    if (incomplete.status === "invalid") {
      expect(incomplete.reason.length).toBeGreaterThan(0);
    }
  });

  it("rejects invalid recovery and non-boolean retryable", () => {
    expect(parseBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      summary: "x",
      severity: "error",
      retryable: "maybe",
    }).status).toBe("invalid");
    expect(parseBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      summary: "x",
      severity: "error",
      retryable: true,
      recovery: { kind: "retry" },
    }).status).toBe("invalid");
    expect(parseBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      summary: "x",
      severity: "error",
      retryable: true,
      recovery: { kind: "warp", label: "Nope" },
    }).status).toBe("invalid");
  });

  it("defaults missing recovery to none and accepts boolean-like retryable", () => {
    const parsed = parseBackendDiagnostic({
      scope: "backend",
      operation: "doctor",
      summary: "ok",
      severity: "warning",
      retryable: "0",
    });
    expect(parsed.status).toBe("ok");
    if (parsed.status === "ok") {
      expect(parsed.diagnostic.retryable).toBe(false);
      expect(parsed.diagnostic.recovery).toEqual({ kind: "none" });
    }
  });

  it("absent clears chrome while invalid publishes a malformed diagnostic (not healthy)", () => {
    publishDiagnostic(authFailureDiagnostic("prior"));
    expect(getActiveDiagnostic()?.code).toBe(AUTH_FAILURE);

    const absent = parseBackendDiagnostic(null);
    expect(absent.status).toBe("absent");
    clearDiagnostic();
    expect(getActiveDiagnostic()).toBeNull();

    const invalid = parseBackendDiagnostic({ summary: "only summary" });
    expect(invalid.status).toBe("invalid");
    if (invalid.status === "invalid") {
      const observed = malformedBackendDiagnostic(invalid.reason);
      publishDiagnostic(observed);
      const active = getActiveDiagnostic();
      expect(active?.code).toBe(MALFORMED_DIAGNOSTIC_WIRE);
      expect(active?.summary).toMatch(/malformed/i);
      expect(active?.detail).toBeTruthy();
      // Must not masquerade as a healthy clear / successful backend diagnostic.
      expect(active?.severity).toBe("warning");
      expect(fromBackendDiagnostic({ summary: "only summary" })).toBeNull();
    }
  });
});

describe("authFailureDiagnostic", () => {
  it("is retryable with a fix-key label", () => {
    const diag = authFailureDiagnostic("OPENAI_API_KEY rejected", { jobId: "j1" });
    expect(diag.code).toBe(AUTH_FAILURE);
    expect(diag.recovery).toEqual({ kind: "retry", label: "Fix key and retry" });
    expect(diag.jobId).toBe("j1");
  });
});

describe("executeDiagnosticRecovery", () => {
  it("invokes retry callback for retry recovery", async () => {
    const retry = vi.fn();
    await executeDiagnosticRecovery(
      authFailureDiagnostic("bad key"),
      retry,
    );
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("auth failure retry relaunches the failed turn instead of only refreshing diagnostics", async () => {
    const retry = vi.fn();
    const diag = authFailureDiagnostic("OPENAI_API_KEY rejected");
    expect(diag.recovery.kind).toBe("retry");
    await executeDiagnosticRecovery(diag, retry);
    expect(retry).toHaveBeenCalledTimes(1);
    expect(diag.code).toBe(AUTH_FAILURE);
  });
});
