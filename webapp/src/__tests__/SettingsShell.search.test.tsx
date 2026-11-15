import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SettingsShell from "../components/SettingsShell";
import { NAV, matchSettingsPages } from "../components/settingsNav";

vi.mock("../components/ModelsSettingsPage", () => ({ default: () => <div data-testid="page-models" /> }));
vi.mock("../components/LocalModelsSettingsPage", () => ({ default: () => <div data-testid="page-local-models" /> }));
vi.mock("../components/SettingsPane", () => ({
  default: ({ section }: { section: string }) => <div data-testid={`page-${section}`} />,
}));
vi.mock("../components/PluginsPane", () => ({ default: () => <div data-testid="page-plugins" /> }));
vi.mock("../components/ArchivedChatsPane", () => ({ default: () => <div data-testid="page-archived" /> }));
vi.mock("../components/AboutSettingsPage", () => ({ default: () => <div data-testid="page-about" /> }));

function renderShell(onClose = vi.fn()) {
  render(<SettingsShell onClose={onClose} onOpenWizard={() => {}} initialPage="models" />);
  const nav = screen.getByRole("navigation", { name: "Settings pages" });
  const pages = () => within(nav).queryAllByRole("button").map((b) => b.textContent);
  const search = screen.getByRole("searchbox", { name: "Search settings" });
  return { nav, pages, search, onClose };
}

afterEach(() => vi.clearAllMocks());

describe("Settings shell search", () => {
  it("lists every page when the search is empty", () => {
    const { pages } = renderShell();
    expect(pages()).toEqual(NAV.map((item) => item.label));
    expect(screen.getByTestId("page-models")).toBeInTheDocument();
  });

  it("filters the sidebar by a setting name and opens the first match", () => {
    const { pages, search } = renderShell();
    fireEvent.change(search, { target: { value: "timeout" } });
    expect(pages()).toEqual(["Safety"]);
    expect(screen.getByTestId("page-safety")).toBeInTheDocument();
    expect(screen.queryByTestId("page-models")).toBeNull();
  });

  it("matches every word, in sidebar order", () => {
    const { pages, search } = renderShell();
    fireEvent.change(search, { target: { value: "turn on" } });
    expect(pages()).toEqual(["Accounts", "Plugins"]);
    expect(screen.getByTestId("page-providers")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "turn on mcp" } });
    expect(pages()).toEqual(["Plugins"]);
    expect(screen.getByTestId("page-plugins")).toBeInTheDocument();
  });

  it("matches the page label and ignores case", () => {
    const { pages, search } = renderShell();
    fireEvent.change(search, { target: { value: "ABOUT" } });
    expect(pages()).toEqual(["About"]);
    expect(screen.getByTestId("page-about")).toBeInTheDocument();
  });

  it("keeps the open page and says so when nothing matches", () => {
    const { pages, search } = renderShell();
    fireEvent.change(search, { target: { value: "zzz-no-such-setting" } });
    expect(pages()).toEqual([]);
    expect(screen.getByTestId("settings-search-empty")).toHaveTextContent("No page matches.");
    expect(screen.getByTestId("page-models")).toBeInTheDocument();
  });

  it("Escape clears the search first and closes Settings second", () => {
    const { pages, search, onClose } = renderShell();
    fireEvent.change(search, { target: { value: "vault" } });
    expect(pages()).toEqual(["General", "Archived chats"]);
    fireEvent.keyDown(search, { key: "Escape" });
    expect(search).toHaveValue("");
    expect(pages()).toEqual(NAV.map((item) => item.label));
    expect(screen.getByTestId("page-general")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("a page click from a filtered sidebar opens that page", () => {
    const { nav, search } = renderShell();
    fireEvent.change(search, { target: { value: "archive" } });
    fireEvent.click(within(nav).getByRole("button", { name: "Archived chats" }));
    expect(screen.getByTestId("page-archived")).toBeInTheDocument();
  });
});

describe("matchSettingsPages", () => {
  it.each([
    ["sign in", ["Accounts"]],
    ["llama.cpp", ["Local Models"]],
    ["output style", ["General"]],
    ["encrypted", ["Safety"]],
    ["completion sound", ["Notifications"]],
    ["hooks", ["Advanced"]],
    ["puppetmaster", ["About"]],
    ["unarchive", ["Archived chats"]],
    ["pilot picker", ["Models"]],
    ["time", ["Safety", "Advanced"]],
    ["on", ["Accounts", "Plugins"]],
  ])("%s finds %j", (query, labels) => {
    expect(matchSettingsPages(query).map((item) => item.label)).toEqual(labels);
  });
});
