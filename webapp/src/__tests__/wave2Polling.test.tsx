import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import StatePane from "../components/StatePane";
import MemoryPane from "../components/MemoryPane";
import SkillsPane from "../components/SkillsPane";
import PluginsLibrary from "../components/PluginsLibrary";
import McpPane from "../components/McpPane";
import { usePolling } from "../lib/usePolling";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";
vi.mock("../lib/api", () => ({ api: {
  memory: vi.fn().mockResolvedValue({ memory: [] }),
  memoryGraph: vi.fn().mockResolvedValue({ nodes: [], edges: [] }),
  skills: vi.fn().mockResolvedValue([]),
  rules: vi.fn().mockResolvedValue([]),
  plugins: vi.fn().mockResolvedValue({ plugins: [] }),
  mcpStart: vi.fn().mockResolvedValue({ ok: true }),
  mcp: vi.fn().mockResolvedValue({ servers: [], tools: [] }),
  mcpCatalog: vi.fn().mockResolvedValue({ catalog: {} }),
  getWikiStatus: vi.fn().mockResolvedValue({ status: "not_configured" }),
  getCodegraph: vi.fn().mockResolvedValue({ status: "none" }),
  environmentReadiness: vi.fn().mockResolvedValue({}),
} }));
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); localStorage.clear(); clearSWRCache(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
it("disabled State retains event listeners without network reads", async () => {
  render(<StatePane artifacts={[]} networkEnabled={false} />);
  act(() => { window.dispatchEvent(new Event("harness-config-changed")); window.dispatchEvent(new Event("harness-expand-mcp")); });
  await advance(20000);
  for (const method of Object.values(api)) expect(method).not.toHaveBeenCalled();
});
it("disabled MCP does not load status or catalog", async () => {
  render(<McpPane networkEnabled={false} />);
  await advance(10000);
  expect(api.mcp).not.toHaveBeenCalled(); expect(api.mcpCatalog).not.toHaveBeenCalled();
});
it.each(["0", "1"])("State has one MCP owner with expansion %s", async (open) => {
  localStorage.setItem("pmharness.statePane.mcpOpen", open);
  render(<StatePane artifacts={[]} />);
  await advance(0); expect(api.mcp).toHaveBeenCalledTimes(1);
  await advance(4000); expect(api.mcp).toHaveBeenCalledTimes(2);
});
it("poll waits for settlement and stops after cleanup", async () => {
  let finish = () => {};
  const poll = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const view = renderHook(() => usePolling(poll, 1000, { backoff: false }));
  await advance(0); await advance(10000); expect(poll).toHaveBeenCalledTimes(1);
  await act(async () => finish()); await advance(1000); expect(poll).toHaveBeenCalledTimes(2);
  view.unmount(); await act(async () => finish()); await advance(10000); expect(poll).toHaveBeenCalledTimes(2);
});
it("poll recovers from rejection and respects enabled and document visibility", async () => {
  const poll = vi.fn().mockRejectedValue(new Error("offline"));
  const view = renderHook(({ enabled }) => usePolling(poll, 1000, { enabled }), { initialProps: { enabled: false } });
  await advance(3000); expect(poll).not.toHaveBeenCalled();
  view.rerender({ enabled: true }); await advance(0); await advance(1000); expect(poll).toHaveBeenCalledTimes(2);
  vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  await advance(4000); expect(poll).toHaveBeenCalledTimes(2);
  vi.restoreAllMocks();
});
it("reenabling a poll waits for its previous request", async () => {
  let finish = () => {};
  const poll = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const view = renderHook(({ enabled }) => usePolling(poll, 1000, { enabled, backoff: false }), { initialProps: { enabled: true } });
  await advance(0);
  view.rerender({ enabled: false }); view.rerender({ enabled: true });
  await advance(0); expect(poll).toHaveBeenCalledTimes(1);
  await act(async () => finish()); await advance(500); expect(poll).toHaveBeenCalledTimes(2);
});

it.each([
  { Pane: MemoryPane, method: "memory", interval: 15000 },
  { Pane: SkillsPane, method: "rules", interval: 5000 },
  { Pane: PluginsLibrary, method: "plugins", interval: 5000 },
] as const)("$method waits for the entire refresh", async ({ Pane, method, interval }) => {
  const read = vi.mocked(api[method]);
  read.mockImplementationOnce(() => new Promise(() => {}));
  render(<Pane />);
  await advance(0); await advance(interval * 4);
  expect(read).toHaveBeenCalledTimes(1);
});
it("Skills applies independent success when the other read rejects", async () => {
  vi.mocked(api.skills).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(api.rules).mockResolvedValueOnce([{ id: "r", text: "Keep success", scope: "global", state: "active" }]);
  render(<SkillsPane />); await advance(0);
  expect(screen.getByText("Keep success")).toBeInTheDocument();
});
it("MCP action publishes refreshed status to its parent", async () => {
  const onStatus = vi.fn();
  vi.mocked(api.mcp).mockResolvedValueOnce({ servers: [{ name: "test", running: false, tools: 0 }], tools: [] });
  render(<McpPane onStatus={onStatus} />); await advance(0);
  const fresh = { servers: [{ name: "test", running: true, tools: 2 }], tools: [{ name: "fresh" }] };
  vi.mocked(api.mcp).mockResolvedValueOnce(fresh);
  fireEvent.click(screen.getByTitle("Start")); await advance(0);
  expect(onStatus).toHaveBeenLastCalledWith(fresh);
});
it("State transfers polling ownership when MCP collapses and expands", async () => {
  const view = render(<StatePane artifacts={[]} />);
  await advance(0);
  fireEvent.click(screen.getByTitle("Hide MCP servers")); await advance(0);
  expect(api.mcp).toHaveBeenCalledTimes(2);
  await advance(4000); expect(api.mcp).toHaveBeenCalledTimes(3);
  fireEvent.click(screen.getByTitle("Show MCP servers")); await advance(0);
  expect(api.mcp).toHaveBeenCalledTimes(4);
  await advance(4000); expect(api.mcp).toHaveBeenCalledTimes(5);
  view.rerender(<StatePane artifacts={[]} networkEnabled={false} />);
  await advance(20000); expect(api.mcp).toHaveBeenCalledTimes(5);
});
it("Memory waits for its graph request too", async () => {
  vi.mocked(api.memoryGraph).mockImplementationOnce(() => new Promise(() => {}));
  render(<MemoryPane />); await advance(0); await advance(60000);
  expect(api.memory).toHaveBeenCalledTimes(1);
  expect(api.memoryGraph).toHaveBeenCalledTimes(1);
});
it("poll invokes the latest callback without restarting its cadence", async () => {
  const first = vi.fn(); const latest = vi.fn();
  const view = renderHook(({ poll }) => usePolling(poll, 1000), { initialProps: { poll: first } });
  await advance(0); view.rerender({ poll: latest }); await advance(999);
  expect(latest).not.toHaveBeenCalled(); await advance(1);
  expect(first).toHaveBeenCalledTimes(1); expect(latest).toHaveBeenCalledTimes(1);
});
