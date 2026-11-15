import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsSettingsPage, { clearCatalogSnapshot, orderModelsEnabledFirst } from "../components/ModelsSettingsPage";
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

  it("pins enabled models above disabled siblings", async () => {
    const mixed: ModelCatalogEntry[] = [
      { spec: "openrouter:off-a", model: "off-a", name: "Off A", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: false },
      { spec: "openrouter:on-b", model: "on-b", name: "On B", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: true },
      { spec: "openrouter:off-c", model: "off-c", name: "Off C", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: false },
      { spec: "openrouter:on-a", model: "on-a", name: "On A", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: true },
    ];
    expect(orderModelsEnabledFirst(mixed).map((row) => row.model)).toEqual(["on-a", "on-b", "off-a", "off-c"]);
    mockModelCatalog.mockResolvedValue({ catalog: mixed, all: mixed, enabled: mixed.filter((row) => row.enabled) });
    render(<ModelsSettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("On A")).toBeInTheDocument();
    });
    const labels = screen.getAllByText(/^(On A|On B|Off A|Off C)$/).map((node) => node.textContent);
    expect(labels).toEqual(["On A", "On B", "Off A", "Off C"]);
  });

  it("shows the picker facts on each model row", async () => {
    const rows = [
      { spec: "anthropic:claude-opus-5-5", model: "claude-opus-5-5", name: "Claude Opus 5.5", provider: "anthropic", provider_display: "Anthropic", available: true, enabled: true, billing: "plan", context_window: 1_000_000, image_input: true },
      { spec: "openrouter:deepseek/v4", model: "deepseek/v4", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: false, price_in: 0.435, price_out: 0.87, context_window: 262144, image_input: false },
      { spec: "openrouter:bare", model: "bare", provider: "openrouter", provider_display: "OpenRouter", available: true, enabled: false },
    ] as ModelCatalogEntry[];
    mockModelCatalog.mockResolvedValue({ catalog: rows, all: rows, enabled: [] });
    render(<ModelsSettingsPage />);
    await waitFor(() => expect(screen.getByText("Claude Opus 5.5")).toBeInTheDocument());
    expect(screen.getByText("Plan · 1M context · Vision")).toBeInTheDocument();
    expect(screen.getByText("$0.44 / $0.87 per MTok · 262k context · Text only")).toBeInTheDocument();
    // A row with no known fact shows no fact line.
    const bare = screen.getAllByTestId("model-row").find((r) => r.textContent?.startsWith("bare"));
    expect(bare?.textContent).toBe("bare");
  });

  it("toggles a model with toggleModel and the switch state", async () => {
    vi.mocked(api.toggleModel).mockResolvedValue({ ok: true, enabled: [] });
    mockModelCatalog.mockResolvedValue({ catalog: sampleCatalog, all: sampleCatalog, enabled: [] });
    render(<ModelsSettingsPage />);
    const toggle = await screen.findByRole("switch", { name: "claude-sonnet" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(toggle);
    await waitFor(() => expect(api.toggleModel).toHaveBeenCalledWith("anthropic:claude-sonnet", false));
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("restores the switch when toggleModel fails", async () => {
    vi.mocked(api.toggleModel).mockRejectedValue(new Error("down"));
    mockModelCatalog.mockResolvedValue({ catalog: sampleCatalog, all: sampleCatalog, enabled: [] });
    render(<ModelsSettingsPage />);
    const toggle = await screen.findByRole("switch", { name: "claude-sonnet" });
    fireEvent.click(toggle);
    await waitFor(() => expect(api.toggleModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
  });

  it("shows only enabled models of a long provider list until Show all", async () => {
    const many: ModelCatalogEntry[] = Array.from({ length: 20 }, (_, i) => ({
      spec: `openrouter:m${i}`, model: `m${i}`, provider: "openrouter", provider_display: "OpenRouter",
      available: true, enabled: i === 3,
    }));
    mockModelCatalog.mockResolvedValue({ catalog: many, all: many, enabled: [] });
    render(<ModelsSettingsPage />);
    await waitFor(() => expect(screen.getByText("m3")).toBeInTheDocument());
    expect(screen.getAllByTestId("model-row")).toHaveLength(1);
    expect(screen.getByText("19 more models")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(screen.getAllByTestId("model-row")).toHaveLength(20);
    // A search always shows every match.
    fireEvent.change(screen.getByPlaceholderText("Search models or providers"), { target: { value: "m1" } });
    expect(screen.getAllByTestId("model-row").map((r) => r.textContent)).toContain("m19");
  });

  it("has no collapsible provider headers", async () => {
    mockModelCatalog.mockResolvedValue({ catalog: sampleCatalog, all: sampleCatalog, enabled: sampleCatalog });
    render(<ModelsSettingsPage />);
    await waitFor(() => expect(screen.getByText("claude-sonnet")).toBeInTheDocument());
    expect(document.querySelector("[aria-expanded]")).toBeNull();
    expect(screen.getByRole("heading", { name: "Anthropic" })).toBeInTheDocument();
  });
});
