import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  collectIntermediateAssistantItems,
  groupAgentActivity,
  transcriptViewportKeys,
  type GroupedItem,
  type Item,
} from "../components/TranscriptList";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function listProps(items: Item[]) {
  return {
    items,
    status: "done" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

function nativeCard(id: string) {
  return {
    type: "card",
    id,
    kind: "run_command",
    goal: `command ${id}`,
    result: { status: "complete", command: `command ${id}` },
  };
}

const NATIVE_STATUSES = [
  "Temporary directory is ready; writing the source files next.",
  "The workspace write was refused, so I am switching to the shell path.",
  "Source and tests are now present; starting the first CLI run.",
  "The first run exposed a header bug; inspecting the parser branch.",
  "The parser fix is in place; rerunning decimal precision cases.",
  "Decimal precision passes; moving on to refund coverage.",
  "Refund totals are correct; exercising quoted customer names.",
  "Quoted names pass; checking whitespace normalization now.",
  "Whitespace handling is sound; testing blank input rows.",
  "Blank rows are ignored; validating malformed amount failures.",
  "Malformed amounts fail cleanly; checking empty customer names.",
  "Empty names are rejected; exercising missing-file behavior.",
  "Missing files exit nonzero without partial JSON output.",
  "The normal suite passes; running the actual executable directly.",
  "Direct execution works; inspecting stderr for the failure cases.",
  "Failure diagnostics are clear; confirming stdout stays empty.",
  "No partial stdout was emitted; checking exact two-place formatting.",
  "Formatting is correct; running the complete regression suite.",
  "The complete suite passes; collecting the observed command results.",
  "Final verification is queued; checking the missing-file case once more.",
] as const;

function bonsaiShapeDisplay() {
  const progress = NATIVE_STATUSES.map((text, index) => [
    { type: "message", role: "assistant", text },
    nativeCard(`tool-${index + 1}`),
  ]).flat();
  return [
    { type: "message", role: "user", text: "Hey" },
    { type: "message", role: "assistant", text: "Hello from Bonsai" },
    { type: "message", role: "user", text: "Build and verify the CLI" },
    nativeCard("tool-0"),
    ...progress,
    { type: "message", role: "assistant", text: "The CLI is complete and verified." },
    { type: "message", role: "user", text: "Check one more thing" },
    {
      type: "message",
      role: "assistant",
      text: "This explicit answer must stay visible.",
      channel: "answer",
    },
    nativeCard("second-tool-0"),
    { type: "message", role: "assistant", text: "Second-turn native status" },
    nativeCard("second-tool-1"),
    { type: "message", role: "assistant", text: "Second turn complete." },
  ];
}

function hydrateAndGroup(display = bonsaiShapeDisplay()): {
  items: Item[];
  grouped: GroupedItem[];
} {
  const items = transcriptResponseToItems({ display });
  const intermediate = collectIntermediateAssistantItems(items, false);
  return { items, grouped: groupAgentActivity(items, intermediate) };
}

describe("native untyped progress stays in reading order", () => {
  // A local model that narrates every step (Bonsai shape: 20 status lines,
  // each followed by one tool). Status prose paints as a Bubble while it
  // streams and is never re-filed into a fold afterwards, so the transcript
  // reads status, tools, status, tools, and nothing jumps when a tool lands.
  it("hydrates each status line top-level with its tools folded after it", () => {
    const { grouped } = hydrateAndGroup();
    const groups = grouped.filter(
      (row): row is Extract<GroupedItem, { kind: "activity_group" }> =>
        row.kind === "activity_group",
    );
    expect(groups).toHaveLength(23);
    expect(groups.every((g) => g.items.every((row) => row.kind === "card"))).toBe(true);

    const visibleMessages = grouped
      .filter((row): row is Extract<GroupedItem, { kind: "msg" }> => row.kind === "msg")
      .map((row) => row.msg.text);
    for (const status of NATIVE_STATUSES) expect(visibleMessages).toContain(status);
    expect(visibleMessages).toContain("The CLI is complete and verified.");
    expect(visibleMessages).toContain("This explicit answer must stay visible.");
    expect(grouped.map((row) => (row.kind === "msg" ? row.msg.text : row.kind)).slice(3, 7)).toEqual([
      "activity_group",
      NATIVE_STATUSES[0],
      "activity_group",
      NATIVE_STATUSES[1],
    ]);
  });

  it("shows every status without expanding and keeps tool evidence behind its fold", () => {
    const { items } = hydrateAndGroup();
    render(<TranscriptList {...listProps(items)} />);
    for (const status of [NATIVE_STATUSES[0], NATIVE_STATUSES[19]]) {
      expect(screen.getByText(status)).toBeVisible();
    }
    expect(screen.queryByText("Ran command tool-1")).toBeNull();
  });

  it("keeps earlier rows and their keys while native progress grows", () => {
    const completeDisplay = bonsaiShapeDisplay();
    const nextTurnIndex = completeDisplay.findIndex((row) => (
      row.type === "message" && "text" in row && row.text === "Check one more thing"
    ));
    const initialDisplay = completeDisplay.slice(0, nextTurnIndex);
    const initial = hydrateAndGroup(initialDisplay);
    const grown = hydrateAndGroup([
      ...initialDisplay.slice(0, -1),
      { type: "message", role: "assistant", text: "Growing native status" },
      nativeCard("tool-21"),
      ...initialDisplay.slice(-1),
    ]);
    // Everything painted before the growth keeps its row and key; the new
    // status and its tool are appended after them.
    const before = transcriptViewportKeys(initial.grouped.slice(0, -1));
    expect(transcriptViewportKeys(grown.grouped.slice(0, before.length))).toEqual(before);

    const { rerender } = render(<TranscriptList {...listProps(initial.items)} />);
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();
    rerender(<TranscriptList {...listProps(grown.items)} />);
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();
  });

  it("never treats untyped prose as progress, whatever follows it", () => {
    const untyped: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Ordinary untyped answer" },
    };
    const card: Item = {
      kind: "card",
      card: { id: "late-tool", goal: "late tool", running: false, open: false },
    };
    for (const open of [true, false]) {
      expect(collectIntermediateAssistantItems([untyped, card], open).has(untyped)).toBe(false);
    }
  });
});
