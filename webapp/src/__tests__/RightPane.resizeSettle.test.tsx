import { act, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn().mockResolvedValue([]),
    swarmLive: vi.fn().mockResolvedValue({ jobs: [] }),
  },
}));
vi.mock("../components/StatePane", () => ({ default: () => <div /> }));
vi.mock("../components/SwarmPane", () => ({ default: () => <div /> }));
vi.mock("../components/BrowserPane", () => ({ default: () => <div /> }));
vi.mock("../components/FileTree", () => ({ default: () => <div /> }));
vi.mock("../components/SourceControl", () => ({ default: () => <div /> }));
vi.mock("../components/WorktreesPane", () => ({ default: () => <div /> }));
vi.mock("../components/SettingsShell", () => ({ default: () => <div /> }));
vi.mock("../components/TerminalPane", () => ({ default: () => <div /> }));
vi.mock("../components/CheckpointsPane", () => ({ default: () => <div /> }));
vi.mock("../components/DiffReviewPane", () => ({ default: () => <div /> }));
vi.mock("../components/EconomicsPane", () => ({ default: () => <div /> }));
vi.mock("../components/ErrorBoundary", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const LAYOUT_KEY = "pmharness.board.cardLayouts.v1";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("a shell drag writes the board layout once after it settles, not on every tick", () => {
  vi.useFakeTimers();
  localStorage.clear();
  localStorage.setItem("pmharness.board.openCards", JSON.stringify(["state", "economics"]));
  localStorage.setItem("pmharness.board.columns.v1", '[["state"],["economics"]]');
  localStorage.setItem(LAYOUT_KEY, '{"state":{"columnSpan":7},"economics":{"columnSpan":5}}');
  let width = 700;
  let notify = () => {};
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { notify = callback; }
    observe() {}
    disconnect() {}
  });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, width, 600));
  render(<RightPane visible artifacts={[]} onOpenWizard={vi.fn()} />);

  const setItem = vi.spyOn(Storage.prototype, "setItem");
  const layoutWrites = () => setItem.mock.calls.filter(([key]) => key === LAYOUT_KEY).length;
  for (const next of [720, 760, 820, 900]) {
    width = next;
    act(() => notify());
  }
  expect(layoutWrites()).toBe(0);

  act(() => { vi.advanceTimersByTime(1000); });
  expect(layoutWrites()).toBe(1);
  const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
  expect(saved.state.columnSpan / 12 * 900).toBeCloseTo(7 / 12 * 700);
});
