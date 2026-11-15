import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AboutSettingsPage from "../components/AboutSettingsPage";
import { api, type Config } from "../lib/api";

vi.mock("../lib/api", () => ({ api: { config: vi.fn() } }));

const config: Config = {
  driver: "anthropic:claude-opus-5-5",
  reach: "anthropic",
  budget: 3,
  package_version: "0.9.671",
  checkout_sha: "54c73a9d0123456789abcdef0123456789abcdef",
  puppetmaster_version: "1.50.0",
};

type Win = { harnessIPC?: unknown };

function setBridge(check: () => Promise<unknown>, openReleases = vi.fn().mockResolvedValue(true)) {
  (window as Win).harnessIPC = { updates: { check, openReleases } };
  return openReleases;
}

function row(testId: string) {
  return within(screen.getByTestId(testId));
}

beforeEach(() => {
  vi.mocked(api.config).mockResolvedValue(config);
});
afterEach(() => {
  delete (window as Win).harnessIPC;
  vi.clearAllMocks();
});

it("shows the version, the checkout commit and the Puppetmaster version from /api/config", async () => {
  render(<AboutSettingsPage />);
  expect(await screen.findByText("v0.9.671")).toBeInTheDocument();
  expect(row("about-commit").getByText("54c73a9d")).toHaveAttribute("title", config.checkout_sha);
  expect(row("about-puppetmaster").getByText("v1.50.0")).toBeInTheDocument();
  expect(api.config).toHaveBeenCalledOnce();
});

it("says Not found when the backend has no Puppetmaster version", async () => {
  vi.mocked(api.config).mockResolvedValue({ ...config, puppetmaster_version: "", checkout_sha: "" });
  render(<AboutSettingsPage />);
  expect(await row("about-puppetmaster").findByText("Not found")).toBeInTheDocument();
  expect(row("about-commit").getByText("Unknown")).toBeInTheDocument();
});

it("says the backend could not be read", async () => {
  vi.mocked(api.config).mockRejectedValue(new Error("offline"));
  render(<AboutSettingsPage />);
  expect(await row("about-version").findByText("Could not read the backend.")).toBeInTheDocument();
});

it("outside the desktop app it says no update check runs", async () => {
  render(<AboutSettingsPage />);
  await screen.findByText("v0.9.671");
  expect(row("about-update").getByText("Not available")).toBeInTheDocument();
  expect(row("about-update").queryByRole("button")).toBeNull();
  expect(screen.queryByTestId("about-releases")).toBeNull();
});

it("checks for updates on open and says up to date", async () => {
  const check = vi.fn().mockResolvedValue({ available: false, behind: 0, branch: "main" });
  setBridge(check);
  render(<AboutSettingsPage />);
  expect(await row("about-update").findByText("Up to date")).toBeInTheDocument();
  expect(check).toHaveBeenCalledOnce();
});

it("names a ready release and checks again on Check", async () => {
  const check = vi.fn()
    .mockResolvedValueOnce({ available: false, behind: 0 })
    .mockResolvedValueOnce({ available: true, behind: 3, latest: "0.9.672", current: "0.9.671", branch: "main" });
  setBridge(check);
  render(<AboutSettingsPage />);
  await row("about-update").findByText("Up to date");
  await act(async () => { fireEvent.click(row("about-update").getByRole("button", { name: "Check" })); });
  expect(row("about-update").getByText("v0.9.672 ready")).toBeInTheDocument();
  expect(check).toHaveBeenCalledTimes(2);
});

it("shows a failed check with its reason", async () => {
  setBridge(vi.fn().mockResolvedValue({ available: false, error: "git fetch failed" }));
  render(<AboutSettingsPage />);
  expect(await row("about-update").findByText("Check failed")).toBeInTheDocument();
  expect(row("about-update").getByText("git fetch failed")).toBeInTheDocument();
});

it("opens the release notes through the update bridge", async () => {
  const openReleases = setBridge(vi.fn().mockResolvedValue({ available: false }));
  render(<AboutSettingsPage />);
  fireEvent.click(await row("about-releases").findByRole("button", { name: "Open" }));
  expect(openReleases).toHaveBeenCalledOnce();
});
