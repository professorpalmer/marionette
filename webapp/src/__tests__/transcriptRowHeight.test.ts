import { describe, expect, it, vi, beforeEach } from "vitest";
import type { GroupedItem } from "../components/TranscriptList";
import {
  assistantTextForMeasure,
  createTranscriptRowHeightCache,
  hasMarkdownCodeFence,
  hasMarkdownImage,
  hasMarkdownTable,
  hasMermaidFence,
  rowMeasureSignal,
  rowNeedsDomMeasure,
  rowPretextSpec,
  shouldAttachDomMeasure,
  shouldRemeasureImmediately,
  stripMarkdownForPretext,
  transcriptBubbleMaxWidth,
  transcriptFeedInnerWidth,
  transcriptRowCacheKey,
  TRANSCRIPT_ROW_FALLBACK_PX,
  TRANSCRIPT_USER_CLAMP_PX,
} from "../components/conversation/transcriptRowHeight";

vi.mock("@chenglou/pretext", () => ({
  prepare: vi.fn((text: string, _font: string, opts?: { whiteSpace?: string }) => ({
    text,
    whiteSpace: opts?.whiteSpace ?? "normal",
  })),
  layout: vi.fn((handle: { text: string }, maxWidth: number, lineHeight: number) => {
    const charsPerLine = Math.max(8, Math.floor(maxWidth / 7));
    const lineCount = Math.max(1, Math.ceil(handle.text.length / charsPerLine));
    return { height: lineCount * lineHeight, lineCount };
  }),
}));

function msg(
  role: "user" | "assistant",
  text: string,
  extra: Partial<{ images: { path: string; name: string; previewUrl: string }[]; workerStream: boolean }> = {},
): GroupedItem {
  return {
    kind: "msg",
    msg: { role, text, ...extra },
  };
}

describe("transcriptRowHeight", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("computes feed inner width from the scroll parent", () => {
    expect(transcriptFeedInnerWidth(1200)).toBe(720);
    expect(transcriptFeedInnerWidth(400)).toBe(352);
    expect(transcriptFeedInnerWidth(200)).toBe(240);
  });

  it("applies role-specific bubble width ratios", () => {
    expect(transcriptBubbleMaxWidth(600, "user")).toBe(510);
    expect(transcriptBubbleMaxWidth(600, "assistant")).toBe(570);
  });

  it("builds stable cache keys from row id, text, and font", () => {
    const a = transcriptRowCacheKey("row-1", "hello", "13px Inter");
    const b = transcriptRowCacheKey("row-1", "hello", "13px Inter");
    const c = transcriptRowCacheKey("row-1", "hello!", "13px Inter");
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });

  it("detects markdown features that require DOM measurement", () => {
    expect(hasMarkdownCodeFence("plain")).toBe(false);
    expect(hasMarkdownCodeFence("```ts\nx\n```")).toBe(true);
    expect(hasMermaidFence("```mermaid\ngraph TD\n```")).toBe(true);
    expect(hasMarkdownImage("![alt](https://x/y.png)")).toBe(true);
    expect(hasMarkdownTable("plain pipes | are not a table")).toBe(false);
    expect(
      hasMarkdownTable("| Repository | Audited ref |\n|---|---|\n| hermes-agent | main |"),
    ).toBe(true);
  });

  it("hides Codex gerund title crumbs the way Investigating headlines already hide", () => {
    expect(
      assistantTextForMeasure(
        "**Creating concise manifest summaries****Preparing audit manifest files**",
      ),
    ).toBe("");
    expect(assistantTextForMeasure("Use **bold** in the real answer.")).toBe(
      "Use **bold** in the real answer.",
    );
  });

  it("classifies rows for Pretext vs DOM settle", () => {
    expect(rowNeedsDomMeasure(msg("user", "short note"))).toBe(false);
    expect(rowNeedsDomMeasure(msg("assistant", "```py\nprint(1)\n```"))).toBe(true);
    expect(
      rowNeedsDomMeasure(msg("assistant", "| A | B |\n|---|---|\n| 1 | 2 |")),
    ).toBe(true);
    expect(rowNeedsDomMeasure(msg("user", "pic", {
      images: [{ path: "/a.png", name: "a", previewUrl: "blob:x" }],
    }))).toBe(true);
    expect(rowNeedsDomMeasure({ kind: "activity_group", items: [] })).toBe(true);
    expect(rowNeedsDomMeasure({ kind: "turn_terminal", cause: "x", state: "y", text: "done" })).toBe(false);
  });

  it("strips fenced markdown before Pretext prose measurement", () => {
    const stripped = stripMarkdownForPretext("# Title\n\nHello **world**\n\n```js\nx\n```");
    expect(stripped).not.toContain("```");
    expect(stripped).toContain("Hello world");
  });

  it("cleans assistant traceback noise for measurement", () => {
    const raw = "Traceback (most recent call last):\n  File x\nValueError: bad\n\nActual answer.";
    expect(assistantTextForMeasure(raw)).toBe("Actual answer.");
  });

  it("estimates prose height via cached prepare+layout", () => {
    const cache = createTranscriptRowHeightCache();
    const item = msg("user", "line one\nline two\nline three");
    const h1 = cache.estimateRowHeight(item, "u-1", 600);
    const h2 = cache.estimateRowHeight(item, "u-1", 600);
    expect(h1).toBeGreaterThan(24);
    expect(h2).toBe(h1);
  });

  it("caps long user messages at the clamp budget", () => {
    const cache = createTranscriptRowHeightCache();
    const wall = "word ".repeat(400);
    const item = msg("user", wall);
    const height = cache.estimateRowHeight(item, "u-wall", 600);
    expect(height).toBeLessThanOrEqual(TRANSCRIPT_USER_CLAMP_PX + 40);
  });

  it("returns fixed heights for chip rows", () => {
    const cache = createTranscriptRowHeightCache();
    const chip: GroupedItem = {
      kind: "turn_terminal",
      cause: "stop",
      state: "done",
      text: "Stopped",
    };
    expect(cache.estimateRowHeight(chip, "t-1", 600)).toBe(36);
  });

  it("falls back when Pretext spec is missing for complex rows", () => {
    const cache = createTranscriptRowHeightCache();
    const row: GroupedItem = { kind: "activity_group", items: [] };
    expect(cache.estimateRowHeight(row, "ag-1", 600)).toBe(TRANSCRIPT_ROW_FALLBACK_PX * 2);
  });

  it("builds Pretext specs for user and assistant bubbles", () => {
    const userSpec = rowPretextSpec(msg("user", "hi"), 600);
    expect(userSpec?.whiteSpace).toBe("pre-wrap");
    expect(userSpec?.maxProsePx).toBe(TRANSCRIPT_USER_CLAMP_PX);

    const assistantSpec = rowPretextSpec(msg("assistant", "Hello **there**"), 600);
    expect(assistantSpec?.whiteSpace).toBe("normal");
    expect(assistantSpec?.text).toContain("Hello there");
  });

  it("gates DOM measureElement until feed settle for rich rows", () => {
    const rich = msg("assistant", "```js\n1\n```");
    expect(shouldAttachDomMeasure(rich, false)).toBe(false);
    expect(shouldAttachDomMeasure(rich, true)).toBe(true);
    expect(shouldAttachDomMeasure(msg("user", "plain"), true)).toBe(false);
    const fold: GroupedItem = { kind: "activity_group", items: [] };
    expect(shouldAttachDomMeasure(fold, false)).toBe(true);
    expect(shouldAttachDomMeasure(fold, true)).toBe(true);
    expect(shouldRemeasureImmediately(fold)).toBe(true);
    expect(shouldRemeasureImmediately(msg("user", "plain"))).toBe(false);
    const streaming: GroupedItem = {
      kind: "msg",
      msg: { role: "assistant", text: "Hel", streaming: true },
    };
    const grown: GroupedItem = {
      kind: "msg",
      msg: { role: "assistant", text: "Hello there", streaming: true },
    };
    expect(shouldRemeasureImmediately(streaming)).toBe(true);
    expect(shouldAttachDomMeasure(streaming, false)).toBe(true);
    expect(rowMeasureSignal(streaming)).not.toBe(rowMeasureSignal(grown));
  });
});
