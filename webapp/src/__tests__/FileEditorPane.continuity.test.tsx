import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FileEditorPane from "../components/FileEditorPane";
import { api } from "../lib/api";
import { desktopBridgeMissingDiagnostic } from "../lib/operationalDiagnostic";
import { publishDiagnostic, resetDiagnosticBus } from "../lib/operationalDiagnosticBus";

const mounts = { count: 0 };

vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn(),
    readFile: vi.fn(),
    writeFile: vi.fn(),
    fileRawUrl: vi.fn((p: string) => `raw:${p}`),
    getReviews: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock("@uiw/react-codemirror", () => ({
  default: function MockCodeMirror({
    value,
    onChange,
  }: {
    value?: string;
    onChange?: (val: string) => void;
  }) {
    useEffect(() => {
      mounts.count += 1;
    }, []);
    return (
      <div>
        <pre data-testid="cm-value">{value || ""}</pre>
        <button type="button" data-testid="cm-dirty" onClick={() => onChange?.(`${value || ""}edit`)}>
          dirty
        </button>
      </div>
    );
  },
}));

describe("FileEditorPane keeps the document on screen", () => {
  beforeEach(() => {
    mounts.count = 0;
    resetDiagnosticBus();
    vi.mocked(api.config).mockResolvedValue({ repo: "/repo" } as any);
    vi.mocked(api.readFile).mockResolvedValue({ ok: true, content: "a\n" } as any);
  });

  afterEach(() => {
    resetDiagnosticBus();
    vi.clearAllMocks();
  });

  it("a failed save leaves the editor mounted with the edit and a Save failed chip", async () => {
    vi.mocked(api.writeFile).mockResolvedValue({ ok: false, error: "EACCES: permission denied" } as any);
    render(<FileEditorPane path="src/a.ts" onClose={() => {}} onDirtyChange={() => {}} />);
    await screen.findByTestId("cm-value");
    fireEvent.click(screen.getByTestId("cm-dirty"));
    fireEvent.click(screen.getByTitle("Save file (Cmd/Ctrl+S)"));

    await screen.findByText("Save failed");
    expect(screen.getByTestId("cm-value").textContent).toBe("a\nedit");
    expect(screen.getByTestId("editor-notice").textContent).toContain("EACCES");
    expect(screen.queryByText("Close editor")).toBeNull();
    expect(mounts.count).toBe(1);
  });

  it("a readiness diagnostic shows a banner above the open editor", async () => {
    render(<FileEditorPane path="src/a.ts" onClose={() => {}} onDirtyChange={() => {}} />);
    await screen.findByTestId("cm-value");
    act(() => {
      publishDiagnostic(desktopBridgeMissingDiagnostic());
    });
    expect(screen.getByTestId("editor-notice").textContent).toContain("Desktop bridge is missing");
    expect(screen.getByTestId("cm-value").textContent).toBe("a\n");
  });

  it("an initial read failure still gets the full-pane notice", async () => {
    vi.mocked(api.readFile).mockResolvedValue({ ok: false, error: "No such file" } as any);
    render(<FileEditorPane path="src/missing.ts" onClose={() => {}} onDirtyChange={() => {}} />);
    await screen.findByText("No such file");
    expect(screen.getByText("Close editor")).toBeTruthy();
    expect(screen.queryByTestId("cm-value")).toBeNull();
  });

  it("switching files keeps the previous document until the new read lands", async () => {
    const { rerender } = render(<FileEditorPane path="src/a.ts" onClose={() => {}} onDirtyChange={() => {}} />);
    await screen.findByTestId("cm-value");

    let resolveB: (v: unknown) => void = () => {};
    vi.mocked(api.readFile).mockReturnValueOnce(new Promise((r) => { resolveB = r; }) as any);
    rerender(<FileEditorPane path="src/b.ts" onClose={() => {}} onDirtyChange={() => {}} />);

    expect(screen.queryByText("Reading file...")).toBeNull();
    expect(screen.getByTestId("cm-value").textContent).toBe("a\n");

    await act(async () => {
      resolveB({ ok: true, content: "b\n" });
    });
    await waitFor(() => expect(screen.getByTestId("cm-value").textContent).toBe("b\n"));
    expect(mounts.count).toBe(1);
  });
});
