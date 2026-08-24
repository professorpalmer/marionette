import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  type Item,
} from "../components/TranscriptList";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function parseTranslateY(transform: string): number | null {
  const match = transform.match(/translateY\(([-\d.]+)px\)/);
  return match ? Number(match[1]) : null;
}

function listProps(
  items: Item[],
  scrollContainerRef: RefObject<HTMLDivElement | null>,
) {
  return {
    items,
    status: "done" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    scrollContainerRef,
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

function longTranscript(count: number): Item[] {
  const items: Item[] = [];
  for (let i = 0; i < count; i++) {
    items.push({
      kind: "msg",
      msg: { role: "user", text: `user turn ${i}` },
    });
    items.push({
      kind: "msg",
      msg: { role: "assistant", text: `assistant reply ${i}` },
    });
  }
  return items;
}

/** TanStack reads offsetHeight first, then RO borderBoxSize — feed both. */
class SizedResizeObserver {
  private readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    const box = { inlineSize: 600, blockSize: 360 };
    const rect = {
      x: 0,
      y: 0,
      width: 600,
      height: 360,
      top: 0,
      left: 0,
      bottom: 360,
      right: 600,
      toJSON() {
        return this;
      },
    };
    this.callback(
      [
        {
          target,
          contentRect: rect as DOMRectReadOnly,
          borderBoxSize: [box],
          contentBoxSize: [box],
          devicePixelContentBoxSize: [box],
        } as ResizeObserverEntry,
      ],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

function sizeFeedElement(el: HTMLDivElement) {
  Object.defineProperty(el, "offsetHeight", {
    configurable: true,
    get: () => 360,
  });
  Object.defineProperty(el, "offsetWidth", {
    configurable: true,
    get: () => 600,
  });
  Object.defineProperty(el, "clientHeight", {
    configurable: true,
    get: () => 360,
  });
  Object.defineProperty(el, "clientWidth", {
    configurable: true,
    get: () => 600,
  });
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => 80 * 72,
  });
  el.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      width: 600,
      height: 360,
      top: 0,
      left: 0,
      bottom: 360,
      right: 600,
      toJSON() {
        return this;
      },
    }) as DOMRect;
}

function VirtualFeedHarness({ items }: { items: Item[] }) {
  const feedRef = useRef<HTMLDivElement>(null);
  const [, setReady] = useState(0);
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    sizeFeedElement(el);
    setReady((n) => n + 1);
  }, []);
  return (
    <div
      ref={feedRef}
      data-testid="virtual-feed"
      style={{ height: 360, overflow: "auto" }}
    >
      <TranscriptList {...listProps(items, feedRef)} />
    </div>
  );
}

describe("transcript feed virtualizer", () => {
  it("does not keep a Show-earlier / RENDER_WINDOW=40 live path", () => {
    const items = longTranscript(50);
    render(
      <TranscriptList
        {...listProps(items, { current: null })}
      />,
    );
    expect(screen.queryByRole("button", { name: /Show earlier messages/i })).toBeNull();
    expect(screen.getByText("user turn 0")).toBeTruthy();
    expect(screen.getByText("user turn 49")).toBeTruthy();
  });

  it("mounts only a viewport-sized window when the feed scroll parent is sized", async () => {
    vi.stubGlobal("ResizeObserver", SizedResizeObserver);
    const items = longTranscript(60);
    render(<VirtualFeedHarness items={items} />);

    await waitFor(() => {
      const rows = screen.getAllByTestId("transcript-virtual-row");
      expect(rows.length).toBeGreaterThan(0);
      expect(rows.length).toBeLessThan(80);
    });

    expect(screen.queryByRole("button", { name: /Show earlier messages/i })).toBeNull();
    expect(screen.getByTestId("transcript-virtual-list")).toBeTruthy();
  });

  it("keeps distinct translateY positions on virtual rows when feed layout motion is on", async () => {
    vi.stubGlobal("ResizeObserver", SizedResizeObserver);

    render(<VirtualFeedHarness items={longTranscript(60)} />);

    await waitFor(() => {
      expect(screen.getAllByTestId("transcript-virtual-row").length).toBeGreaterThan(1);
    });

    const translateYs = screen
      .getAllByTestId("transcript-virtual-row")
      .map((row) => parseTranslateY(row.style.transform))
      .filter((value): value is number => value !== null);

    expect(translateYs.length).toBeGreaterThan(1);
    expect(new Set(translateYs).size).toBeGreaterThan(1);
  });

  it("keeps the virtual window after the scroll parent reports 0 height", async () => {
    vi.stubGlobal("ResizeObserver", SizedResizeObserver);
    const items = longTranscript(60);
    const { rerender } = render(<VirtualFeedHarness items={items} />);

    await waitFor(() => {
      const rows = screen.getAllByTestId("transcript-virtual-row");
      expect(rows.length).toBeGreaterThan(0);
      expect(rows.length).toBeLessThan(80);
    });

    const feed = screen.getByTestId("virtual-feed") as HTMLDivElement;
    Object.defineProperty(feed, "clientHeight", { configurable: true, get: () => 0 });
    Object.defineProperty(feed, "offsetHeight", { configurable: true, get: () => 0 });
    rerender(<VirtualFeedHarness items={items} />);

    const list = screen.getByTestId("transcript-virtual-list");
    expect(list.className).toContain("relative");
    expect(list.className).not.toContain("flex-col");
    expect(screen.queryAllByTestId("transcript-virtual-row").length).toBeLessThan(80);
  });
});
