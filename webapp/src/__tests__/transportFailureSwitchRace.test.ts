import { afterEach, expect, it } from "vitest";
import { getActiveDiagnostic, resetDiagnosticBus } from "../lib/operationalDiagnosticBus";
import { publishTransportFailure } from "../lib/transportFailure";

afterEach(() => resetDiagnosticBus());

const httpError = (status: number, body: unknown) =>
  Object.assign(new Error(`/api/x -> ${status}`), { status, body });

it.each([
  { code: "view_changed" },
  { ok: false, code: "session_changed", error: "active session changed" },
  { error: "session changed or missing" },
])("a read superseded by a session switch raises no diagnostic: %o", (body) => {
  publishTransportFailure(httpError(409, body), { operation: "getJSON", path: "/api/x", sessionId: "b" });
  expect(getActiveDiagnostic()).toBeNull();
});

it("other conflicts still surface", () => {
  publishTransportFailure(httpError(409, { code: "pilot_not_ready", error: "not ready" }), { operation: "getJSON", path: "/api/x" });
  expect(getActiveDiagnostic()?.severity).toBe("error");
});
