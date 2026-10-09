import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  collectIntermediateAssistantItems,
  groupAgentActivity,
  transcriptViewportKeys,
  wrapSealedTurns,
  type GroupedItem,
  type Item,
} from "../components/TranscriptList";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function listProps(items: Item[], live = false) {
  return {
    items,
    status: (live ? "streaming" : "done") as "streaming" | "done",
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: live,
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

describe("native untyped progress: stream order live, one Worked for fold at seal", () => {
  // A local model that narrates every step (Bonsai shape: 20 status lines,
  // each followed by one tool). Status prose paints as a Bubble while it
  // streams, and each tool lands in its own fold below it. When the turn
  // ends, the statuses and their folds go under one Worked for fold, and the
  // final answer paints below it.
  it("seals each turn into one Worked for fold with its final answer below", () => {
    const { grouped } = hydrateAndGroup();
    const sealed = wrapSealedTurns(grouped, false);
    const works = sealed.filter(
      (row): row is Extract<GroupedItem, { kind: "turn_work" }> => row.kind === "turn_work",
    );
    expect(works).toHaveLength(2);
    const firstGroups = works[0].rows.filter((row) => row.kind === "activity_group");
    expect(firstGroups).toHaveLength(21);
    expect(works[0].rows.filter((row) => row.kind === "msg").map((row) => row.kind === "msg" && row.msg.text))
      .toEqual([...NATIVE_STATUSES]);
    expect(works[1].rows.map((row) => row.kind)).toEqual(["activity_group", "msg", "activity_group"]);

    const flat = sealed.map((row) => (row.kind === "msg" ? row.msg.text : row.kind));
    const build = flat.indexOf("Build and verify the CLI");
    expect(flat.slice(build + 1)).toEqual([
      "turn_work",
      "The CLI is complete and verified.",
      "Check one more thing",
      "This explicit answer must stay visible.",
      "turn_work",
      "Second turn complete.",
    ]);
  });

  it("keeps statuses behind Worked for and tool evidence behind its own fold", () => {
    const { items } = hydrateAndGroup();
    render(<TranscriptList {...listProps(items)} />);
    expect(screen.getByText("The CLI is complete and verified.")).toBeVisible();
    expect(screen.getByText("This explicit answer must stay visible.")).toBeVisible();
    expect(screen.queryByText(NATIVE_STATUSES[0])).toBeNull();
    fireEvent.click(screen.getAllByTestId("turn-work-fold")[0].querySelector("button")!);
    for (const status of [NATIVE_STATUSES[0], NATIVE_STATUSES[19]]) {
      expect(screen.getByText(status)).toBeVisible();
    }
    expect(screen.queryByTestId("activity-rows")).toBeNull();
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

    const { rerender } = render(<TranscriptList {...listProps(initial.items, true)} />);
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();
    rerender(<TranscriptList {...listProps(grown.items, true)} />);
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
