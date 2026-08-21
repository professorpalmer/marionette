import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsSettingsPage, { clearCatalogSnapshot } from "../components/ModelsSettingsPage";
import { api, type ModelCatalogEntry } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    modelCatalog: vi.fn(),
    toggleModel: vi.fn(),
  },
}));

const mockModelCatalog = vi.mocked(api.modelCatalog);

const CATALOG_SNAPSHOT_KEY = "pmharness.models.catalogSnapshot";

const sampleCatalog: ModelCatalogEntry[] = [
  {
    spec: "anthropic:claude-sonnet",
    model: "claude-sonnet",
    provider: "anthropic",
    provider_display: "Anthropic",
    available: true,
    enabled: true,
  },
];

const oxCatalog: ModelCatalogEntry[] = [
  {
    spec: "opencode-zen:x-preview-f-free",
    model: "x-preview-f-free",
    name: "Ox Alpha Free",
    provider: "opencode-zen",
    provider_display: "OpenCode Zen",
    available: true,
    enabled: false,
  },
  {
    spec: "opencode-zen:big-pickle",
    model: "big-pickle",
    name: "Big Pickle",
    provider: "opencode-zen",
    provider_display: "OpenCode Zen",
    available: true,
    enabled: false,
  },
];

const oxGoCatalog: ModelCatalogEntry[] = [
  {
    spec: "opencode-go:ox-alpha-free",
    model: "ox-alpha-free",
    name: "Ox Alpha Free",
    provider: "opencode-go",
    provider_display: "OpenCode Go",
    available: true,
    enabled: false,
  },
  {
    spec: "opencode-go:deepseek-v4-flash",
    model: "deepseek-v4-flash",
    provider: "opencode-go",
    provider_display: "OpenCode Go",
    available: true,
    enabled: false,
  },
];

describe("ModelsSettingsPage cached first paint", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearCatalogSnapshot();
  });

  afterEach(() => {
    clearCatalogSnapshot();
  });

  it("renders cached catalog immediately without a loading flash", async () => {
    localStorage.setItem(
      CATALOG_SNAPSHOT_KEY,
      JSON.stringify({ catalog: sampleCatalog, savedAt: Date.now() }),
    );
    mockModelCatalog.mockImplementation(
      () => new Promise(() => {}),
    );

    render(<ModelsSettingsPage />);

    expect(screen.getByText("claude-sonnet")).toBeInTheDocument();
    expect(screen.queryByText("Loading model catalog...")).toBeNull();
  });

  it("retains cached catalog when revalidation fails", async () => {
    localStorage.setItem(
      CATALOG_SNAPSHOT_KEY,
      JSON.stringify({ catalog: sampleCatalog, savedAt: Date.now() }),
    );
    mockModelCatalog.mockRejectedValue(new Error("network"));

    render(<ModelsSettingsPage />);

    expect(screen.getByText("claude-sonnet")).toBeInTheDocument();

    await waitFor(() => {
      expect(mockModelCatalog).toHaveBeenCalled();
    });

    expect(screen.getByText("claude-sonnet")).toBeInTheDocument();
    expect(screen.queryByText("Loading model catalog...")).toBeNull();
  });

  it("shows loading only when no snapshot exists", async () => {
    mockModelCatalog.mockImplementation(
      () => new Promise(() => {}),
    );

    render(<ModelsSettingsPage />);

    expect(screen.getByText("Loading model catalog...")).toBeInTheDocument();
  });

  it("force-refreshes once when Models is first opened", async () => {
    localStorage.setItem(
      CATALOG_SNAPSHOT_KEY,
      JSON.stringify({ catalog: sampleCatalog, savedAt: Date.now() }),
    );
    mockModelCatalog.mockResolvedValue({
      catalog: sampleCatalog,
      all: sampleCatalog,
      enabled: [],
    });

    const first = render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(mockModelCatalog).toHaveBeenCalledWith({ refresh: true });
    });
    first.unmount();
    mockModelCatalog.mockClear();
    mockModelCatalog.mockResolvedValue({
      catalog: sampleCatalog,
      all: sampleCatalog,
      enabled: [],
    });

    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(mockModelCatalog).toHaveBeenCalled();
    });
    expect(mockModelCatalog).toHaveBeenCalledWith({ refresh: false });
  });

  it("clearCatalogSnapshot resets the once-per-session refresh latch", async () => {
    mockModelCatalog.mockResolvedValue({
      catalog: sampleCatalog,
      all: sampleCatalog,
      enabled: [],
    });
    const first = render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(mockModelCatalog).toHaveBeenCalledWith({ refresh: true });
    });
    first.unmount();
    clearCatalogSnapshot();
    mockModelCatalog.mockClear();
    mockModelCatalog.mockResolvedValue({
      catalog: sampleCatalog,
      all: sampleCatalog,
      enabled: [],
    });

    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(mockModelCatalog).toHaveBeenCalledWith({ refresh: true });
    });
  });

  it("searches and displays the Ox Alpha friendly name", async () => {
    mockModelCatalog.mockResolvedValue({
      catalog: oxCatalog,
      all: oxCatalog,
      enabled: [],
    });

    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("Ox Alpha Free")).toBeInTheDocument();
    });
    expect(screen.getByText("x-preview-f-free")).toBeInTheDocument();

    const search = screen.getByPlaceholderText("Search models or providers");
    fireEvent.change(search, { target: { value: "Ox Alpha" } });
    expect(screen.getByText("Ox Alpha Free")).toBeInTheDocument();
    expect(screen.queryByText("Big Pickle")).toBeNull();
  });

  it("searches and displays Ox Alpha Free for a live OpenCode Go row", async () => {
    mockModelCatalog.mockResolvedValue({
      catalog: oxGoCatalog,
      all: oxGoCatalog,
      enabled: [],
    });

    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("Ox Alpha Free")).toBeInTheDocument();
    });
    expect(screen.getByText("ox-alpha-free")).toBeInTheDocument();

    const search = screen.getByPlaceholderText("Search models or providers");
    fireEvent.change(search, { target: { value: "Ox Alpha" } });
    expect(screen.getByText("Ox Alpha Free")).toBeInTheDocument();
    expect(screen.queryByText("deepseek-v4-flash")).toBeNull();
  });

  it("searches dynamic catalog names like MiMo", async () => {
    const mimoCatalog: ModelCatalogEntry[] = [
      ...oxCatalog,
      {
        spec: "opencode-zen:mimo-v2.5-free",
        model: "mimo-v2.5-free",
        name: "MiMo-V2.5 Free",
        provider: "opencode-zen",
        provider_display: "OpenCode Zen",
        available: true,
        enabled: false,
      },
    ];
    mockModelCatalog.mockResolvedValue({
      catalog: mimoCatalog,
      all: mimoCatalog,
      enabled: [],
    });

    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("MiMo-V2.5 Free")).toBeInTheDocument();
    });

    const search = screen.getByPlaceholderText("Search models or providers");
    fireEvent.change(search, { target: { value: "MiMo" } });
    expect(screen.getByText("MiMo-V2.5 Free")).toBeInTheDocument();
    expect(screen.queryByText("Ox Alpha Free")).toBeNull();
    expect(screen.queryByText("Big Pickle")).toBeNull();
  });
});
