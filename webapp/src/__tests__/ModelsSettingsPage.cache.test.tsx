import { render, screen, waitFor } from "@testing-library/react";
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
    spec: "anthropic/claude-sonnet",
    model: "claude-sonnet",
    provider: "anthropic",
    provider_display: "Anthropic",
    available: true,
    enabled: true,
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
});
