import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  type Item,
} from "../components/TranscriptList";
import {
  partitionStackedActivity,
  ranCommandsLabel,
  thoughtFoldLabel,
  workedForLabel,
} from "../lib/turnProgress";
import {
  DEFAULT_SESSION_TITLE,
  deriveSessionTitle,
  displaySessionListTitle,
} from "../lib/sessionTitle";
import { isActivityHeadlineText } from "../lib/sessionTitleLock";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function sealedCommand(id: string, goal: string, durationMs = 2000): Extract<Item, { kind: "card" }> {
  return {
    kind: "card",
    card: {
      id,
      goal,
      cwd: null,
      kind: "run_command",
      running: false,
      open: false,
      result: { status: "ok", duration_ms: durationMs, command: goal },
    },
  };
}

function listProps(items: Item[]) {
  return {
    items,
    status: "idle" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    holdSwarmAwait: false,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

describe("stacked fold labels", () => {
  it("formats Worked for / Thought / Ran chrome", () => {
    expect(workedForLabel(23_000)).toBe("Worked for 23s");
    expect(workedForLabel(6 * 60_000)).toBe("Worked for 6m");
    expect(thoughtFoldLabel({ live: true })).toBe("Thinking…");
    expect(thoughtFoldLabel({ durationMs: 8_000 })).toBe("Thought 8s");
    expect(ranCommandsLabel(1)).toBe("Ran 1 command");
    expect(ranCommandsLabel(3)).toBe("Ran 3 commands");
  });

  it("nests Thought inside a Ran commands partition", () => {
    const items = [
      { kind: "thinking" as const },
      { kind: "card", cardKind: "run_command" },
      { kind: "thinking" as const },
      { kind: "card", cardKind: "run_command" },
    ];
    const rows = partitionStackedActivity(items, (row) => ({
      cardKind: row.kind === "card" ? row.cardKind : null,
      isThinking: row.kind === "thinking",
    }));
    expect(rows.map((r) => r.kind)).toEqual(["thought", "commands"]);
    expect(rows[1]?.kind).toBe("commands");
    if (rows[1]?.kind !== "commands") return;
    expect(rows[1].items).toHaveLength(3);
    expect(rows[1].items.filter((it) => it.kind === "thinking")).toHaveLength(1);
  });
});

describe("sealed stacked folds (Worked for + Thought + Ran)", () => {
  it("shows Worked for + finale Bubble; Thought separate; finale never inside fold", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "fix the redirect bug" } },
      {
        kind: "thinking",
        text: "looking at ActionForm",
        id: "th-outer",
        duration_ms: 8000,
      },
      sealedCommand("c1", "git status", 1500),
      {
        kind: "thinking",
        text: "checking status output",
        id: "th-nested",
        duration_ms: 2000,
      },
      sealedCommand("c2", "rg ActionForm", 1500),
      {
        kind: "msg",
        msg: {
          role: "assistant",
          text: "The redirect was missing a return. Regular weight finale.",
        },
      },
    ];

    render(<TranscriptList {...listProps(items)} />);

    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Explored/i)).toBeNull();
    expect(screen.queryByText(/Investigating/i)).toBeNull();

    // Finale stays a top-level Bubble — visible without expanding Worked for.
    expect(
      screen.getByText(/The redirect was missing a return/i),
    ).toBeTruthy();
    const finale = screen.getByText(/The redirect was missing a return/i);
    expect(finale.closest(".transcript-msg-body")?.className).toMatch(/font-normal/);

    // Thought / Ran stay collapsed until the Worked for row opens.
    expect(screen.queryByTestId("thought-fold")).toBeNull();
    expect(screen.queryByTestId("ran-commands-fold")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Worked for/i }));
    expect(screen.getAllByTestId("thought-fold").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId("ran-commands-fold")).toBeTruthy();
    expect(screen.getByText(/^Thought/)).toBeTruthy();
    expect(screen.getByText(/Ran 2 commands/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Ran 2 commands/i }));
    expect(screen.getByText(/Ran git status/i)).toBeTruthy();
    // Nested Thought lives inside the Ran fold.
    const ranFold = screen.getByTestId("ran-commands-fold");
    expect(ranFold.querySelectorAll('[data-testid="thought-fold"]').length).toBeGreaterThanOrEqual(1);
  });
});

describe("session title lock", () => {
  it("never derives titles from investigating / Explored / Diagnosing walls", () => {
    expect(deriveSessionTitle("Investigating ActionForm redirect…")).toBe(
      DEFAULT_SESSION_TITLE,
    );
    expect(deriveSessionTitle("Explored 1 search, 3 commands")).toBe(
      DEFAULT_SESSION_TITLE,
    );
    expect(deriveSessionTitle("Diagnosing production error…")).toBe(
      DEFAULT_SESSION_TITLE,
    );
    expect(deriveSessionTitle("Planning call queries with CodeGraph")).toBe(
      DEFAULT_SESSION_TITLE,
    );
    expect(deriveSessionTitle("Stopped.")).toBe(DEFAULT_SESSION_TITLE);
    expect(deriveSessionTitle("fix the redirect in ActionForm")).toBe(
      "Fix the redirect in ActionForm",
    );
  });

  it("40 investigating headlines still map to one user-derived list title", () => {
    const userTitle = deriveSessionTitle("debug flash builder redirect");
    expect(userTitle).toBe("Debug flash builder redirect");

    const headlines = Array.from({ length: 40 }, (_, i) =>
      i % 2 === 0
        ? `Explored ${i + 1} search, 3 commands`
        : `Investigating ActionForm step ${i}`,
    );
    for (const h of headlines) {
      expect(isActivityHeadlineText(h)).toBe(true);
      expect(displaySessionListTitle(h)).toBe("Untitled");
    }
    // One session row — display stays the human title, never the wall.
    expect(displaySessionListTitle(userTitle)).toBe(userTitle);
    expect(displaySessionListTitle(headlines.join("\n"))).toBe("Untitled");
  });
});
