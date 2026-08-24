import { describe, expect, it } from "vitest";
import {
  DESKTOP_BRIDGE_MISSING,
  TRANSPORT_HTTP,
  TRANSPORT_UNCERTAIN,
  CONVERSATION_TURN_FAILURE,
  belongsToActiveScope,
  classifyFailure,
  conversationLifecycleAfterFailure,
  conversationTurnFailureDiagnostic,
  createOperationalDiagnostic,
  desktopBridgeMissing,
  desktopBridgeMissingDiagnostic,
  desktopShellExpected,
  fromTransportFailure,
  isConversationTurnFailureDiagnostic,
  isOperationalDiagnostic,
  isUncertainTransport,
  nextDiagnostic,
  panelNotice,
  resolveRepaired,
  sameRoot,
  sanitizeDiagnosticText,
  sharedReadinessNotice,
} from "../lib/operationalDiagnostic";
import {
  getActiveDiagnostic,
  publishDiagnostic,
  resetDiagnosticBus,
} from "../lib/operationalDiagnosticBus";

describe("classifyFailure", () => {
  it("keeps form, upload, and validation local", () => {
    expect(classifyFailure({ kind: "form" })).toBe("local");
    expect(classifyFailure({ kind: "upload" })).toBe("local");
    expect(classifyFailure({ kind: "validation" })).toBe("local");
  });

  it("treats readiness and transport failures as operational", () => {
    expect(classifyFailure({ kind: "operational", scope: "prompt_queue" })).toBe("operational");
    expect(classifyFailure({ scope: "desktop_bridge" })).toBe("operational");
  });
});

describe("sanitizeDiagnosticText", () => {
  it("redacts bearer and api-key material", () => {
    expect(sanitizeDiagnosticText("Authorization: Bearer sk-abc123456789")).toContain("[redacted]");
    expect(sanitizeDiagnosticText("api_key=secret-value")).toContain("[redacted]");
    expect(sanitizeDiagnosticText("Authorization: Bearer sk-abc123456789")).not.toMatch(/sk-abc/);
  });

  it("keeps a multi-line auth error inside the budget", () => {
    const out = sanitizeDiagnosticText("Error:\nanthropic.AuthenticationError: invalid x-api-key");
    expect(out).toContain("anthropic.AuthenticationError");
    expect(out.length).toBeLessThanOrEqual(160);
  });

  it("bounds length", () => {
    expect(sanitizeDiagnosticText("x".repeat(200)).length).toBeLessThanOrEqual(160);
  });

  it("redacts AIza, GitHub token, and JWT shapes", () => {
    const google = "AIzaSyD-abcdefghijklmnopqrstuvwxyz012345";
    const ghp = "ghp_" + "A".repeat(36);
    const pat = "github_pat_" + "B".repeat(36);
    const jwt =
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjw46_OwfmRMV94B7vjYTbxqgVwOKCsAXEzXkOfBk";
    expect(sanitizeDiagnosticText(`key ${google}`)).toContain("[redacted]");
    expect(sanitizeDiagnosticText(`key ${google}`)).not.toContain(google);
    expect(sanitizeDiagnosticText(`key ${google}`)).not.toMatch(/AIza/);
    expect(sanitizeDiagnosticText(`tok ${ghp}`)).toContain("[redacted]");
    expect(sanitizeDiagnosticText(`tok ${ghp}`)).not.toContain(ghp);
    expect(sanitizeDiagnosticText(`tok ${pat}`)).toContain("[redacted]");
    expect(sanitizeDiagnosticText(`tok ${pat}`)).not.toContain(pat);
    expect(sanitizeDiagnosticText(`jwt ${jwt}`)).toContain("[redacted]");
    expect(sanitizeDiagnosticText(`jwt ${jwt}`)).not.toContain(jwt);
    expect(sanitizeDiagnosticText(`jwt ${jwt}`)).not.toMatch(/eyJ/);
  });
});

describe("desktop bridge detection", () => {
  it("expects a desktop shell from an Electron userAgent", () => {
    expect(desktopShellExpected({ userAgent: "Mozilla/5.0 Electron/39.0.0" })).toBe(true);
    expect(desktopShellExpected({ userAgent: "Mozilla/5.0 Chrome/128" })).toBe(false);
    expect(desktopShellExpected({ shellFlag: true, userAgent: "Chrome" })).toBe(true);
  });

  it("flags the preload-crash case: shell expected, harnessIPC missing", () => {
    expect(
      desktopBridgeMissing({
        userAgent: "Mozilla/5.0 Electron/39.0.0",
        hasBridge: false,
      }),
    ).toBe(true);
    expect(
      desktopBridgeMissing({
        userAgent: "Mozilla/5.0 Electron/39.0.0",
        hasBridge: true,
      }),
    ).toBe(false);
    expect(
      desktopBridgeMissing({
        userAgent: "Mozilla/5.0 Chrome/128",
        hasBridge: false,
      }),
    ).toBe(false);
  });
});

describe("desktopBridgeMissingDiagnostic", () => {
  it("is one scoped root cause with a real recovery and known-safe data", () => {
    const diag = desktopBridgeMissingDiagnostic({ operation: "startup" });
    expect(isOperationalDiagnostic(diag)).toBe(true);
    expect(diag.scope).toBe("desktop_bridge");
    expect(diag.code).toBe(DESKTOP_BRIDGE_MISSING);
    expect(diag.summary).toBe("Desktop bridge is missing");
    expect(diag.dataSafe).toBe(true);
    expect(diag.retryable).toBe(false);
    expect(diag.recovery).toEqual({ kind: "relaunch", label: "Relaunch Marionette" });
    expect(diag.summary.toLowerCase()).not.toBe("error");
  });
});

describe("fromTransportFailure", () => {
  it("collapses a desktop-without-bridge failure to the preload diagnostic", () => {
    const diag = fromTransportFailure({
      operation: "getJSON",
      path: "/api/workspaces",
      err: new Error("Failed to fetch"),
      userAgent: "Electron/39.0.0",
      hasBridge: false,
    });
    expect(diag.code).toBe(DESKTOP_BRIDGE_MISSING);
    expect(diag.scope).toBe("desktop_bridge");
  });

  it("keeps transient connection flaps uncertain instead of inventing a root cause", () => {
    const diag = fromTransportFailure({
      operation: "getJSON",
      err: new Error("connect ECONNREFUSED 127.0.0.1:49376"),
      isTransient: true,
      userAgent: "Chrome",
      hasBridge: false,
    });
    expect(diag.code).toBe(TRANSPORT_UNCERTAIN);
    expect(diag.retryable).toBe(true);
    expect(isUncertainTransport(diag)).toBe(true);
  });

  it("records a web HTTP failure without claiming the desktop bridge died", () => {
    const diag = fromTransportFailure({
      operation: "refreshQueue",
      path: "/api/prompt-queue",
      err: new Error("/api/prompt-queue -> 500"),
      userAgent: "Chrome",
      hasBridge: false,
    });
    expect(diag.code).toBe(TRANSPORT_HTTP);
    expect(diag.scope).toBe("transport");
  });
});

describe("scope and retry rules", () => {
  it("does not attach a session-scoped diagnostic after a session switch", () => {
    const diag = createOperationalDiagnostic({
      scope: "conversation",
      operation: "send",
      summary: "Turn failed",
      severity: "error",
      retryable: true,
      sessionId: "sess-a",
    });
    expect(belongsToActiveScope(diag, { sessionId: "sess-a" })).toBe(true);
    expect(belongsToActiveScope(diag, { sessionId: "sess-b" })).toBe(false);
  });

  it("does not let uncertain transport erase a known failure", () => {
    const known = desktopBridgeMissingDiagnostic();
    const flap = fromTransportFailure({
      operation: "getJSON",
      isTransient: true,
      userAgent: "Chrome",
      hasBridge: false,
    });
    expect(nextDiagnostic(known, flap)).toBe(known);
    expect(nextDiagnostic(null, flap)).toBe(flap);
  });

  it("clears only the diagnostic a successful retry repaired", () => {
    const queue = createOperationalDiagnostic({
      scope: "prompt_queue",
      operation: "refresh",
      code: TRANSPORT_HTTP,
      summary: "Could not refresh prompt queue",
      severity: "error",
      retryable: true,
    });
    const other = desktopBridgeMissingDiagnostic();
    expect(resolveRepaired(queue, queue)).toBeNull();
    expect(resolveRepaired(other, queue)).toBe(other);
    expect(sameRoot(queue, { id: "other", code: TRANSPORT_HTTP, scope: "prompt_queue", operation: "refresh" })).toBe(true);
  });
});

describe("shared readiness copy", () => {
  it("replaces unrelated empty/error labels with the desktop-bridge root", () => {
    const diag = desktopBridgeMissingDiagnostic();
    expect(sharedReadinessNotice("No projects", diag)).toBe("Desktop bridge is missing");
    expect(sharedReadinessNotice("No folder", diag)).toBe("Desktop bridge is missing");
    expect(sharedReadinessNotice("Couldn’t refresh prompt queue", diag)).toBe("Desktop bridge is missing");
    expect(sharedReadinessNotice("No projects", null)).toBe("No projects");
  });

  it("lets a panel keep local operational copy unless a readiness root exists", () => {
    expect(panelNotice("Failed to get workspace files", null)).toBe("Failed to get workspace files");
    expect(panelNotice("Failed to get workspace files", desktopBridgeMissingDiagnostic())).toBe(
      "Desktop bridge is missing",
    );
    const files = createOperationalDiagnostic({
      scope: "panel",
      operation: "readDir",
      summary: "Workspace files could not be listed",
      severity: "error",
      retryable: true,
    });
    expect(panelNotice("Failed to get workspace files", files, "panel")).toBe(
      "Workspace files could not be listed",
    );
    expect(panelNotice("Failed to load settings", files, "config")).toBe("Failed to load settings");
  });

  it("treats a failed turn as settled lifecycle plus diagnostic", () => {
    expect(conversationLifecycleAfterFailure()).toBe("idle");
  });
});

describe("diagnostic bus", () => {
  it("keeps one desktop-bridge root when later uncertain transport arrives", () => {
    resetDiagnosticBus();
    publishDiagnostic(desktopBridgeMissingDiagnostic());
    const flap = fromTransportFailure({
      operation: "getJSON",
      isTransient: true,
      userAgent: "Chrome",
      hasBridge: false,
    });
    publishDiagnostic(flap);
    expect(getActiveDiagnostic()?.code).toBe(DESKTOP_BRIDGE_MISSING);
    resetDiagnosticBus();
  });
});

describe("conversationTurnFailureDiagnostic", () => {
  it("is retryable conversation scope with Trace correlation", async () => {
    const { setCorrelationId } = await import("../lib/correlationId");
    setCorrelationId("trace-turn-1");
    const diag = conversationTurnFailureDiagnostic("[error] provider rejected");
    expect(diag.code).toBe(CONVERSATION_TURN_FAILURE);
    expect(diag.scope).toBe("conversation");
    expect(diag.operation).toBe("turn");
    expect(diag.summary).toBe("provider rejected");
    expect(diag.recovery).toEqual({ kind: "retry", label: "Retry" });
    expect(diag.correlationId).toBe("trace-turn-1");
    expect(isConversationTurnFailureDiagnostic(diag)).toBe(true);
    setCorrelationId("");
  });
});

describe("createOperationalDiagnostic", () => {
  it("sanitizes caller-supplied summary and detail", () => {
    const diag = createOperationalDiagnostic({
      scope: "config",
      operation: "save",
      summary: "Authorization: Bearer sk-abcdefghijklmnopqrst",
      detail: "line1\napi_key=supersecret",
      severity: "error",
      retryable: true,
    });
    expect(diag.summary).not.toMatch(/sk-abcd/);
    expect(diag.detail).toContain("line1");
    expect(diag.detail).not.toContain("supersecret");
    expect(diag.recovery).toEqual({ kind: "none" });
  });

  it("inherits the active client correlation id when none is supplied", async () => {
    const { getCorrelationId, setCorrelationId } = await import("../lib/correlationId");
    setCorrelationId("client-corr-abc");
    const diag = createOperationalDiagnostic({
      scope: "conversation",
      operation: "send",
      summary: "Turn failed",
      severity: "error",
      retryable: true,
    });
    expect(diag.correlationId).toBe("client-corr-abc");
    expect(getCorrelationId()).toBe("client-corr-abc");
    setCorrelationId("");
  });
});
