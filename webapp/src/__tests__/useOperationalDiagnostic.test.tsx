import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { desktopBridgeMissingDiagnostic } from "../lib/operationalDiagnostic";
import {
  publishDiagnostic,
  resetDiagnosticBus,
} from "../lib/operationalDiagnosticBus";
import { useOperationalDiagnostic } from "../lib/useOperationalDiagnostic";

describe("useOperationalDiagnostic", () => {
  afterEach(() => {
    resetDiagnosticBus();
  });

  it("starts null and follows publish/clear on the bus", () => {
    const { result } = renderHook(() => useOperationalDiagnostic());
    expect(result.current).toBeNull();

    const diag = desktopBridgeMissingDiagnostic({ operation: "startup" });
    act(() => {
      publishDiagnostic(diag);
    });
    expect(result.current?.code).toBe(diag.code);
    expect(result.current?.summary).toBe(diag.summary);

    act(() => {
      resetDiagnosticBus();
    });
    expect(result.current).toBeNull();
  });
});
