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
import column from "../components/conversation/ConversationChatColumn.tsx?raw";
import helpers from "../components/conversation/feedMotion.tsx?raw";
import list from "../components/TranscriptList.tsx?raw";
import pkgJson from "../../package.json?raw";
import { TranscriptList, type Item } from "../components/TranscriptList";
import {
  feedLayoutMotionEnabled,
  VIRTUAL_ROW_LAYOUT_ENABLED,
} from "../components/conversation/feedMotion";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function listProps(
  items: Item[],
  scrollContainerRef: RefObject<HTMLDivElement | null> = { current: null },
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

function parseTranslateY(transform: string): number | null {
  const match = /translateY\(([-\d.]+)px\)/.exec(transform);
  return match ? Number(match[1]) : null;
}

describe("feed Motion (v0.9.329)", () => {
  it("declares the official motion package in webapp dependencies", () => {
    const pkg = JSON.parse(pkgJson) as {
      dependencies?: Record<string, string>;
      version?: string;
    };
    expect(pkg.dependencies?.motion).toBeTruthy();
    expect(pkg.dependencies?.["framer-motion"]).toBeUndefined();
    expect(pkg.version).toBe("0.9.329");
  });

  it("mounts transcript rows inside motion layout shells", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "hello" } },
          { kind: "msg", msg: { role: "assistant", text: "hi there" } },
        ])}
      />,
    );
    expect(screen.getByTestId("transcript-virtual-list")).toBeTruthy();
    expect(screen.getByText("hello")).toBeTruthy();
    expect(screen.getByText("hi there")).toBeTruthy();
  });

  it("disables feed layout motion when prefers-reduced-motion is set", () => {
    expect(feedLayoutMotionEnabled(true)).toBe(false);
    expect(feedLayoutMotionEnabled(false)).toBe(true);
    expect(feedLayoutMotionEnabled(null)).toBe(true);
  });

  it("keeps layoutScroll on the feed scrollport and popLayout on in-flow presence", () => {
    expect(column).toContain("layoutScroll");
    expect(column).toContain("[overflow-anchor:auto]");
    expect(column).toContain("[scroll-padding-bottom:var(--feed-chrome-clearance");
    expect(column).not.toContain("overflow-anchor:none");
    expect(helpers).toContain('mode="popLayout"');
    expect(helpers).toContain("VIRTUAL_ROW_LAYOUT_ENABLED = false");
    expect(VIRTUAL_ROW_LAYOUT_ENABLED).toBe(false);
    expect(list).toContain("FeedMotionPresence");
    expect(list).toContain("VIRTUAL_ROW_LAYOUT_ENABLED");
    expect(list).toContain("useVirtualizer");
    expect(list).toContain("transcript-live-tail");
    expect(list).not.toMatch(
      /useVirtualWindow[\s\S]*<FeedMotionPresence>[\s\S]*virtualItems\.map/,
    );
    expect(list).not.toMatch(/from ["']node:/);
    expect(list).not.toMatch(/@stylexjs|create\(|stylex\./);
    expect(helpers).not.toMatch(/from ["']node:/);
  });

  it("keeps distinct virtualizer translateY while feed layout is on", async () => {
    expect(feedLayoutMotionEnabled(false)).toBe(true);
    vi.stubGlobal("ResizeObserver", SizedResizeObserver);
    render(<VirtualFeedHarness items={longTranscript(40)} />);

    await waitFor(() => {
      const rows = screen.getAllByTestId("transcript-virtual-row");
      expect(rows.length).toBeGreaterThan(1);
    });

    const rows = screen.getAllByTestId("transcript-virtual-row");
    const ys = rows.map((row) => parseTranslateY((row as HTMLElement).style.transform));
    expect(ys.every((y) => y != null)).toBe(true);
    expect(new Set(ys).size).toBe(ys.length);
    expect(ys.some((y) => y !== 0)).toBe(true);
  });
});
