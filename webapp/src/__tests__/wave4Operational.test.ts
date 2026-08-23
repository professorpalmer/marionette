import { describe, expect, it, vi } from "vitest";
import {
  AUTH_FAILURE,
  BACKEND_NOT_READY,
  authFailureDiagnostic,
  fromBackendDiagnostic,
} from "../lib/operationalDiagnostic";

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
    });
    expect(diag?.scope).toBe("backend");
    expect(diag?.recovery).toEqual({ kind: "retry", label: "Retry" });
  });

  it("returns null for incomplete wire payloads", () => {
    expect(fromBackendDiagnostic({ summary: "only summary" })).toBeNull();
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
    const { executeDiagnosticRecovery } = await import("../lib/operationalRecovery");
    const retry = vi.fn();
    await executeDiagnosticRecovery(
      authFailureDiagnostic("bad key"),
      retry,
    );
    expect(retry).toHaveBeenCalledTimes(1);
  });
});
