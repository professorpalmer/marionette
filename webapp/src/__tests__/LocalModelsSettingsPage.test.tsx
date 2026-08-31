import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LocalModelsSettingsPage from "../components/LocalModelsSettingsPage";
import { api, type LocalModelsSnapshot } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      getLocalModels: vi.fn(),
      localModelCommand: vi.fn(),
      getLocalModelEvents: vi.fn(),
      watchLocalModelEvents: vi.fn(() => () => {}),
    },
  };
});

const getLocalModels = vi.mocked(api.getLocalModels);
const localModelCommand = vi.mocked(api.localModelCommand);
const getLocalModelEvents = vi.mocked(api.getLocalModelEvents);
const watchLocalModelEvents = vi.mocked(api.watchLocalModelEvents);

function snapshot(overrides: Partial<LocalModelsSnapshot> = {}): LocalModelsSnapshot {
  return {
    hardware: {
      os: "Darwin",
      arch: "arm64",
      platform_key: "macos-arm64",
      ram_bytes: 16 * 1024 ** 3,
      disk_free_bytes: 40 * 1024 ** 3,
      accelerator: "metal",
      supported: true,
    },
    catalog: {
      runtime_release: "b10442",
      models: [{
        id: "qwen3-4b",
        name: "Qwen3 4B",
        size: 2497280256,
        context_length: 40960,
        min_ram_gb: 6,
        min_disk_bytes: 3200000000,
        source: "Qwen/Qwen3-4B-GGUF (Apache-2.0)",
        trust: "first-party",
      }],
      model: { id: "qwen3-4b", name: "Qwen3 4B", size: 2497280256, context_length: 40960 },
    },
    managed: {
      runtime: { status: "absent" },
      model: { status: "absent" },
      process: null,
      downloads: {},
      usable: false,
      spec: "local:managed/qwen3-4b",
    },
    externals: [],
    active_spec: "",
    usable_specs: [],
    event_cursor: 0,
    ...overrides,
  };
}

describe("LocalModelsSettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    watchLocalModelEvents.mockImplementation(() => () => {});
    getLocalModelEvents.mockResolvedValue({
      ok: true,
      events: [],
      cursor: 0,
      snapshot: snapshot(),
    });
  });

  it("shows a neutral catalog selector with factual fields", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    render(<LocalModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("local-models-hardware").textContent).toContain("macos-arm64");
    });
    expect(screen.getByTestId("local-models-page").textContent).not.toMatch(/Recommend/i);
    expect(screen.getByTestId("local-models-catalog-select")).toBeTruthy();
    expect(screen.getByTestId("local-models-catalog-facts").textContent).toContain("Qwen3 4B");
    expect(screen.getByTestId("local-models-catalog-facts").textContent).toContain("first-party");
    expect(screen.getByTestId("local-models-catalog-facts").textContent).toContain("40960");
    expect(screen.getByTestId("local-models-remote-copy").textContent).toMatch(/RunPod/i);
    expect(screen.getByRole("button", { name: /Install/i })).toBeTruthy();
    expect(screen.getByTestId("local-models-empty-external").textContent).toMatch(/model id/i);
  });

  it("installs the selected catalog model_id", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "downloading" },
        model: { status: "downloading" },
        downloads: { model: { bytes: 10, total: 100, phase: "download" } },
        spec: "local:managed/qwen3-4b",
      },
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Install/i }));
    fireEvent.click(screen.getByRole("button", { name: /Install/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "install",
        target: "all",
        model_id: "qwen3-4b",
      });
      expect(screen.getByTestId("local-models-progress")).toBeTruthy();
      expect(screen.getByRole("button", { name: /Cancel/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({ type: "cancel", target: "all" });
    });
  });

  it("starts a stopped ready install", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "ready", id: "qwen3-4b" },
        process: null,
        usable: false,
        spec: "local:managed/qwen3-4b",
      },
    }));
    localModelCommand.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "ready" },
        process: { pid: 9, port: 8765, healthy: true, context_length: 40960 },
        usable: true,
        spec: "local:managed/qwen3-4b",
      },
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Start/i }));
    fireEvent.click(screen.getByRole("button", { name: /Start/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({ type: "start" });
    });
  });

  it("starts, activates, and removes a ready install", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "ready", id: "qwen3-4b" },
        process: { pid: 9, port: 8765, healthy: true, context_length: 40960 },
        usable: true,
        spec: "local:managed/qwen3-4b",
      },
    }));
    localModelCommand.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "ready" },
        process: { pid: 9, port: 8765, healthy: true, context_length: 40960 },
        spec: "local:managed/qwen3-4b",
      },
      active_spec: "local:managed/qwen3-4b",
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Activate/i }));
    fireEvent.click(screen.getByRole("button", { name: /Activate/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "activate",
        spec: "local:managed/qwen3-4b",
      });
    });
    fireEvent.click(screen.getByRole("button", { name: /Stop/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({ type: "stop" });
    });
    fireEvent.click(screen.getByRole("button", { name: /Restart/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({ type: "restart" });
    });
    fireEvent.click(screen.getByRole("button", { name: /Remove all/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({ type: "remove", target: "all" });
    });
  });

  it("probes, saves, activates, and deletes an external endpoint", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand
      .mockResolvedValueOnce({
        ok: true,
        url: "http://127.0.0.1:11434/v1",
        vendor: "ollama",
        models: ["llama3"],
        context_length: 4096,
      })
      .mockResolvedValue(snapshot({
        externals: [{
          id: "ollama-127-0-0-1-11434",
          vendor: "ollama",
          base_url: "http://127.0.0.1:11434/v1",
          models: ["llama3"],
          selected_model: "llama3",
          healthy: true,
        }],
      }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Probe/i }));
    fireEvent.click(screen.getByRole("button", { name: /Probe/i }));
    await waitFor(() => {
      expect(screen.getByTestId("local-models-probe").textContent).toContain("ollama");
    });
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    await waitFor(() => {
      expect(screen.getByTestId("local-external-ollama-127-0-0-1-11434")).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /^Activate$/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "activate",
        spec: "local:ollama-127-0-0-1-11434/llama3",
      });
    });
    fireEvent.click(screen.getByRole("button", { name: /Delete/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "remove",
        target: "all",
        endpoint_id: "ollama-127-0-0-1-11434",
      });
    });
  });

  it("saves a RunPod HTTPS endpoint with confirmation and a manual model", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockResolvedValue(snapshot({
      externals: [{
        id: "openai-compatible-proxy-runpod-net-8000",
        name: "runpod-qwen",
        vendor: "openai-compatible",
        base_url: "https://proxy.runpod.net/v1",
        models: ["qwen3-4b"],
        selected_model: "qwen3-4b",
        healthy: true,
      }],
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByLabelText(/Endpoint URL/i));
    fireEvent.change(screen.getByLabelText(/Endpoint URL/i), {
      target: { value: "https://proxy.runpod.net/v1" },
    });
    fireEvent.change(screen.getByLabelText(/Display name/i), {
      target: { value: "runpod-qwen" },
    });
    fireEvent.change(screen.getByLabelText(/Model id/i), {
      target: { value: "qwen3-4b" },
    });
    fireEvent.click(screen.getByTestId("local-models-accept-remote").querySelector("input")!);
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "save_external",
        url: "https://proxy.runpod.net/v1",
        api_key: "",
        accept_lan: false,
        accept_remote: true,
        model: "qwen3-4b",
        name: "runpod-qwen",
      });
    });
  });

  it("surfaces rejection when a public remote is saved without confirmation", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockRejectedValue(new Error("This is a public remote host. Confirm you trust this HTTPS service."));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByLabelText(/Model id/i));
    fireEvent.change(screen.getByLabelText(/Endpoint URL/i), {
      target: { value: "https://proxy.runpod.net/v1" },
    });
    fireEvent.change(screen.getByLabelText(/Model id/i), {
      target: { value: "qwen3-4b" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    await waitFor(() => {
      expect(screen.getByTestId("local-models-error").textContent).toMatch(/trust/i);
    });
    expect(localModelCommand).toHaveBeenCalledWith(expect.objectContaining({
      type: "save_external",
      accept_remote: false,
    }));
  });

  it("never renders a pasted API key in the page", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockResolvedValueOnce({
      ok: true,
      url: "http://127.0.0.1:8080/v1",
      vendor: "llama.cpp",
      models: ["phi"],
    });
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByLabelText(/Optional API key/i));
    fireEvent.change(screen.getByLabelText(/Optional API key/i), {
      target: { value: "sk-secret-value" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Probe/i }));
    await waitFor(() => {
      expect(screen.getByTestId("local-models-probe")).toBeTruthy();
    });
    expect(screen.getByTestId("local-models-page").textContent).not.toContain("sk-secret-value");
  });

  it("shows unsupported hardware without an install button", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      hardware: {
        os: "Darwin",
        arch: "arm64",
        platform_key: "macos-arm64",
        accelerator: "cpu",
        supported: false,
        unsupported_reason: "This machine has 4.0 GB RAM; catalog models need at least 6 GB.",
      },
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("local-models-unsupported").textContent).toContain("4.0 GB RAM");
    });
    expect(screen.getByRole("button", { name: /Install/i })).toBeDisabled();
  });

  it("watches events without an interval owner and applies snapshots immediately", async () => {
    watchLocalModelEvents.mockImplementation((opts) => {
      opts.onEvent({
        kind: "snapshot",
        cursor: 1,
        snapshot: snapshot({
          managed: {
            runtime: { status: "downloading" },
            model: { status: "downloading" },
            downloads: { model: { bytes: 50, total: 100, phase: "download" } },
            spec: "local:managed/qwen3-4b",
          },
        }),
      });
      return () => {};
    });
    getLocalModels.mockResolvedValue(snapshot());
    render(<LocalModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("local-models-progress")).toBeTruthy();
    });
    expect(watchLocalModelEvents).toHaveBeenCalled();
    expect(getLocalModelEvents).not.toHaveBeenCalled();
  });

  it("shows paused progress and a Resume action", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "paused" },
        downloads: { model: { bytes: 40, total: 100, phase: "download" } },
        spec: "local:managed/qwen3-4b",
      },
    }));
    localModelCommand.mockResolvedValue(snapshot());
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Resume/i }));
    expect(screen.getByTestId("local-models-progress")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Resume/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "install",
        target: "all",
        model_id: "qwen3-4b",
      });
    });
  });

  it("offers separate remove actions when components exist", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "ready" },
        model: { status: "ready", id: "qwen3-4b" },
        process: null,
        spec: "local:managed/qwen3-4b",
      },
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Remove model/i }));
    expect(screen.getByRole("button", { name: /Remove runtime/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Remove all/i })).toBeTruthy();
  });

  it("uses a nonempty manual model id over the discovered selection", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand
      .mockResolvedValueOnce({
        ok: true,
        url: "http://127.0.0.1:11434/v1",
        vendor: "ollama",
        models: ["llama3"],
      })
      .mockResolvedValue(snapshot());
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Probe/i }));
    fireEvent.click(screen.getByRole("button", { name: /Probe/i }));
    await waitFor(() => screen.getByTestId("local-models-probe"));
    fireEvent.change(screen.getByLabelText(/Model id/i), {
      target: { value: "custom-qwen" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith(expect.objectContaining({
        type: "save_external",
        model: "custom-qwen",
      }));
    });
  });

  it("clears the API key field after a successful save", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockResolvedValue(snapshot({
      externals: [{
        id: "ollama-127-0-0-1-11434",
        vendor: "ollama",
        base_url: "http://127.0.0.1:11434/v1",
        models: ["llama3"],
        selected_model: "llama3",
        healthy: true,
      }],
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByLabelText(/Optional API key/i));
    fireEvent.change(screen.getByLabelText(/Model id/i), {
      target: { value: "llama3" },
    });
    fireEvent.change(screen.getByLabelText(/Optional API key/i), {
      target: { value: "sk-secret-value" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    await waitFor(() => {
      expect((screen.getByLabelText(/Optional API key/i) as HTMLInputElement).value).toBe("");
    });
    expect(screen.getByTestId("local-models-page").textContent).not.toContain("sk-secret-value");
  });

  it("rejects older GET and SSE snapshots once a newer cursor is owned", async () => {
    let onEvent: ((ev: { kind: string; cursor?: number; snapshot?: LocalModelsSnapshot }) => void) | undefined;
    watchLocalModelEvents.mockImplementation((opts) => {
      onEvent = opts.onEvent;
      return () => {};
    });
    getLocalModels.mockResolvedValue(snapshot({ event_cursor: 0 }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-models-managed-status"));
    onEvent?.({
      kind: "snapshot",
      cursor: 4,
      snapshot: snapshot({
        event_cursor: 4,
        managed: {
          runtime: { status: "ready" },
          model: { status: "ready" },
          process: { pid: 9, port: 1, healthy: true },
          spec: "local:managed/qwen3-4b",
        },
      }),
    });
    await waitFor(() => {
      expect(screen.getByTestId("local-models-managed-status").textContent).toMatch(/Server running/i);
    });
    onEvent?.({
      kind: "snapshot",
      cursor: 1,
      snapshot: snapshot({
        event_cursor: 1,
        managed: {
          runtime: { status: "absent" },
          model: { status: "absent" },
          spec: "local:managed/qwen3-4b",
        },
      }),
    });
    expect(screen.getByTestId("local-models-managed-status").textContent).toMatch(/Server running/i);
  });

  it("clears a stale load error when a live snapshot arrives", async () => {
    getLocalModels.mockRejectedValue(new Error("Could not load local models"));
    let onEvent: ((ev: { kind: string; cursor?: number; snapshot?: LocalModelsSnapshot }) => void) | undefined;
    watchLocalModelEvents.mockImplementation((opts) => {
      onEvent = opts.onEvent;
      return () => {};
    });
    render(<LocalModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("local-models-error").textContent).toMatch(/Could not load/i);
    });
    onEvent?.({
      kind: "snapshot",
      cursor: 2,
      snapshot: snapshot({ event_cursor: 2 }),
    });
    await waitFor(() => {
      expect(screen.queryByTestId("local-models-error")).toBeNull();
      expect(screen.getByTestId("local-models-hardware")).toBeTruthy();
    });
  });

  it("surfaces managed background install errors in the existing alert", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      managed: {
        runtime: { status: "error", error: "checksum failed" },
        model: { status: "absent" },
        spec: "local:managed/qwen3-4b",
      },
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("local-models-error").textContent).toContain("checksum failed");
    });
  });

  it("cleans up poll fallback on unmount", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    let onError: (() => void) | undefined;
    const stopWatch = vi.fn();
    watchLocalModelEvents.mockImplementation((opts) => {
      onError = opts.onError;
      return stopWatch;
    });
    getLocalModelEvents.mockResolvedValue({
      ok: true,
      events: [],
      cursor: 1,
      snapshot: snapshot({ event_cursor: 1 }),
    });
    const { unmount } = render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-models-hardware"));
    onError?.();
    await waitFor(() => {
      expect(getLocalModelEvents).toHaveBeenCalled();
    });
    const calls = getLocalModelEvents.mock.calls.length;
    unmount();
    expect(stopWatch).toHaveBeenCalled();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(getLocalModelEvents.mock.calls.length).toBe(calls);
  });

  it("starts polling fallback only once", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    let onError: (() => void) | undefined;
    let onDone: (() => void) | undefined;
    watchLocalModelEvents.mockImplementation((opts) => {
      onError = opts.onError;
      onDone = opts.onDone;
      return () => {};
    });
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-models-hardware"));
    onError?.();
    onDone?.();
    onError?.();
    await waitFor(() => {
      expect(getLocalModelEvents).toHaveBeenCalledTimes(1);
    });
  });

  it("surfaces command errors in text, not color alone", async () => {
    getLocalModels.mockResolvedValue(snapshot());
    localModelCommand.mockRejectedValue(new Error("Cloud metadata endpoints are blocked"));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByRole("button", { name: /Probe/i }));
    fireEvent.click(screen.getByRole("button", { name: /Probe/i }));
    await waitFor(() => {
      expect(screen.getByTestId("local-models-error").textContent).toContain("metadata");
    });
  });

  it("sends verify_tool_calling and renders capability status", async () => {
    const saved = {
      id: "ollama-127-0-0-1-11434",
      vendor: "ollama",
      base_url: "http://127.0.0.1:11434/v1",
      models: ["llama3"],
      selected_model: "llama3",
      healthy: true,
      tool_calling: { status: "unverified" as const, reason: "", checked_at: null },
    };
    getLocalModels.mockResolvedValue(snapshot({ externals: [saved] }));
    localModelCommand.mockResolvedValue(snapshot({
      externals: [{
        ...saved,
        tool_calling: {
          status: "verified",
          reason: "This model returned a tool call.",
          checked_at: 1,
        },
      }],
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-external-ollama-127-0-0-1-11434"));
    expect(screen.getByTestId("local-models-tool-calling-copy").textContent).toMatch(
      /records whether the endpoint returned the requested tool call/i,
    );
    expect(screen.getByTestId("local-models-tool-calling-copy").textContent).toMatch(
      /does not execute tools or enroll the model as a Puppetmaster swarm worker/i,
    );
    expect(screen.getByTestId("local-models-tool-calling-copy").textContent).not.toMatch(
      /lets this pilot/i,
    );
    expect(screen.getByTestId("local-external-tool-calling-ollama-127-0-0-1-11434").textContent)
      .toMatch(/Unverified/);
    fireEvent.click(screen.getByRole("button", { name: /Test tool calling/i }));
    await waitFor(() => {
      expect(localModelCommand).toHaveBeenCalledWith({
        type: "verify_tool_calling",
        spec: "local:ollama-127-0-0-1-11434/llama3",
      });
      expect(screen.getByTestId("local-external-tool-calling-ollama-127-0-0-1-11434").textContent)
        .toMatch(/Verified/);
    });
    expect(screen.getByTestId("local-models-page").textContent).not.toMatch(/Recommend/i);
  });

  it("renders unsupported and error without blocking activate", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      externals: [{
        id: "ollama-127-0-0-1-11434",
        vendor: "ollama",
        base_url: "http://127.0.0.1:11434/v1",
        models: ["llama3"],
        selected_model: "llama3",
        healthy: true,
        tool_calling: {
          status: "unsupported",
          reason: "This model replied with text instead of a tool call.",
          checked_at: 1,
        },
      }],
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-external-tool-calling-ollama-127-0-0-1-11434"));
    expect(screen.getByTestId("local-external-tool-calling-ollama-127-0-0-1-11434").textContent)
      .toMatch(/Unsupported/);
    expect(screen.getByTestId("local-external-tool-calling-ollama-127-0-0-1-11434").textContent)
      .toMatch(/text instead of a tool call/);
    expect(screen.getByRole("button", { name: /^Activate$/i })).not.toBeDisabled();
  });

  it("renders an error capability status with a concise reason", async () => {
    getLocalModels.mockResolvedValue(snapshot({
      externals: [{
        id: "runpod-box",
        vendor: "openai-compatible",
        base_url: "https://proxy.runpod.net/v1",
        models: ["qwen"],
        selected_model: "qwen",
        healthy: true,
        tool_calling: {
          status: "error",
          reason: "This public endpoint requires an API key.",
          checked_at: 2,
        },
      }],
    }));
    render(<LocalModelsSettingsPage />);
    await waitFor(() => screen.getByTestId("local-external-tool-calling-runpod-box"));
    expect(screen.getByTestId("local-external-tool-calling-runpod-box").textContent).toMatch(/Error/);
    expect(screen.getByTestId("local-external-tool-calling-runpod-box").textContent).toMatch(/API key/);
    expect(screen.getByRole("button", { name: /^Activate$/i })).not.toBeDisabled();
  });
});
