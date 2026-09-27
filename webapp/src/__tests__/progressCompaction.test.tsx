import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

describe("native untyped progress compaction", () => {
  it("hydrates a Bonsai-shaped transcript into one compact activity group per user turn", () => {
    const { grouped } = hydrateAndGroup();

    expect(grouped.map((row) => row.kind)).toEqual([
      "msg",
      "msg",
      "msg",
      "activity_group",
      "msg",
      "msg",
      "msg",
      "activity_group",
      "msg",
    ]);
    const groups = grouped.filter(
      (row): row is Extract<GroupedItem, { kind: "activity_group" }> =>
        row.kind === "activity_group",
    );
    expect(groups).toHaveLength(2);
    expect(groups[0].items.filter((row) => row.kind === "msg")).toHaveLength(20);
    expect(groups[0].items.filter((row) => row.kind === "card")).toHaveLength(21);
    expect(groups[1].items.map((row) => row.kind)).toEqual(["card", "msg", "card"]);

    const visibleMessages = grouped
      .filter((row): row is Extract<GroupedItem, { kind: "msg" }> => row.kind === "msg")
      .map((row) => row.msg.text);
    expect(visibleMessages).toContain("Hello from Bonsai");
    expect(visibleMessages).toContain("The CLI is complete and verified.");
    expect(visibleMessages).toContain("This explicit answer must stay visible.");
    expect(visibleMessages).toContain("Second turn complete.");
  });

  it("shows only the latest status while closed and preserves status plus tool evidence on expand", () => {
    const { items } = hydrateAndGroup();
    render(<TranscriptList {...listProps(items)} />);

    const folds = screen.getAllByTestId("activity-fold");
    expect(folds).toHaveLength(2);
    expect(screen.getByText(NATIVE_STATUSES[19])).toBeVisible();
    expect(screen.queryByText(NATIVE_STATUSES[0])).toBeNull();
    expect(screen.queryByText("command tool-1")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: new RegExp(NATIVE_STATUSES[19]) }));
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();
    const commandFold = screen.getAllByTestId("ran-commands-fold")[0];
    expect(commandFold).toBeDefined();
    if (!commandFold) return;
    fireEvent.click(within(commandFold).getByRole("button"));
    expect(screen.getByText("Ran command tool-0")).toBeVisible();
  });

  it("keeps the activity row identity and manual-open state while native progress grows", () => {
    const completeDisplay = bonsaiShapeDisplay();
    const nextTurnIndex = completeDisplay.findIndex((row) => (
      row.type === "message" && "text" in row && row.text === "Check one more thing"
    ));
    expect(nextTurnIndex).toBeGreaterThan(0);
    const initialDisplay = completeDisplay.slice(0, nextTurnIndex);
    const initial = hydrateAndGroup(initialDisplay);
    const firstGroup = initial.grouped.find((row) => row.kind === "activity_group");
    expect(firstGroup).toBeDefined();
    if (!firstGroup) return;
    const initialKey = transcriptViewportKeys([firstGroup])[0];

    const { rerender } = render(<TranscriptList {...listProps(initial.items)} />);
    fireEvent.click(screen.getByRole("button", { name: new RegExp(NATIVE_STATUSES[19]) }));
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();

    const grown = hydrateAndGroup([
      ...initialDisplay.slice(0, -1),
      { type: "message", role: "assistant", text: "Growing native status" },
      nativeCard("tool-21"),
      ...initialDisplay.slice(-1),
    ]);
    const grownFirstGroup = grown.grouped.find((row) => row.kind === "activity_group");
    expect(grownFirstGroup).toBeDefined();
    if (!grownFirstGroup) return;
    expect(transcriptViewportKeys([grownFirstGroup])[0]).toBe(initialKey);
    if (firstGroup.kind !== "activity_group" || grownFirstGroup.kind !== "activity_group") return;
    expect(grownFirstGroup.items).toHaveLength(firstGroup.items.length + 2);

    rerender(<TranscriptList {...listProps(grown.items)} />);
    expect(screen.getByText(NATIVE_STATUSES[0])).toBeVisible();
    expect(screen.getByText("Growing native status")).toBeVisible();
  });

  it("does not infer progress across answer, user, steer, or question boundaries", () => {
    const untyped: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Ordinary untyped answer" },
    };
    const explicitAnswer: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "Explicit answer", channel: "answer" },
    };
    const card: Item = {
      kind: "card",
      card: { id: "late-tool", goal: "late tool", running: false, open: false },
    };
    const boundaries: Item[] = [
      { kind: "msg", msg: { role: "user", text: "next" } },
      { kind: "steer", text: "change course" },
      {
        kind: "command_approval",
        id: "approval",
        command: "echo ok",
        commandHash: "a".repeat(64),
        sessionId: "session",
        workspaceRoot: "/repo",
        category: "shell",
        reason: "confirm",
        matched: "echo",
        status: "pending",
      },
    ];

    expect(collectIntermediateAssistantItems([untyped, card], false).has(untyped)).toBe(true);
    expect(collectIntermediateAssistantItems([explicitAnswer, card], false).has(explicitAnswer)).toBe(false);
    for (const boundary of boundaries) {
      expect(
        collectIntermediateAssistantItems([untyped, boundary, card], false).has(untyped),
      ).toBe(false);
    }
  });
});
