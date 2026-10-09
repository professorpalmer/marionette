import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  type Item,
} from "../components/TranscriptList";
import {
  partitionActivityRows,
  swarmDoneFoldLabel,
  thoughtFoldLabel,
  workFoldLabel,
  workedForLabel,
  isWorkingEllipsisFallback,
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
  it("formats Worked for / Thought chrome", () => {
    expect(workedForLabel(23_000)).toBe("Worked for 23s");
    expect(workedForLabel(6 * 60_000)).toBe("Worked for 6m");
    expect(workedForLabel(0)).toBe("");
    expect(workedForLabel(null)).toBe("");
    expect(workedForLabel(500)).toBe("Worked for 1s");
    expect(workedForLabel(0)).not.toMatch(/0s/);
    expect(workedForLabel(500)).not.toMatch(/0s/);
    expect(thoughtFoldLabel({ live: true })).toBe("Thinking…");
    expect(thoughtFoldLabel({ durationMs: 8_000 })).toBe("Thought 8s");
    expect(swarmDoneFoldLabel(8, "done")).toBe("Swarm done · 8");
    expect(swarmDoneFoldLabel(1, "done")).toBe("Swarm done");
    expect(swarmDoneFoldLabel(2, "failed")).toBe("Swarm failed · 2");
  });

  it("work fold never falls through to spoken-prose Working...", () => {
    expect(isWorkingEllipsisFallback("Working...")).toBe(true);
    expect(isWorkingEllipsisFallback("Working..")).toBe(true);
    expect(workFoldLabel({ live: true })).toBe("Investigating…");
    expect(workFoldLabel({ live: true, headline: "Working..." })).toBe("Investigating…");
    expect(workFoldLabel({ live: true, headline: "Investigating · git status" })).toBe(
      "Investigating · git status",
    );
    expect(workFoldLabel({ live: true, pausePoint: true })).toBe("Still working…");
    expect(workFoldLabel({ live: false, durationMs: 12_000 })).toBe("Worked for 12s");
  });

  it("keeps tool rows flat between thoughts (no Ran N sub-fold)", () => {
    const items = [
      { kind: "thinking" as const },
      { kind: "card" as const },
      { kind: "thinking" as const },
      { kind: "card" as const },
    ];
    const rows = partitionActivityRows(items, (row) => ({ isThinking: row.kind === "thinking" }));
    expect(rows.map((r) => r.kind)).toEqual(["thought", "item", "thought", "item"]);
  });

  it("coalesces consecutive terminal swarm_pending into one Swarm done fold", () => {
    const items = [
      { kind: "thinking" as const },
      { kind: "swarm_pending" as const, terminal: true },
      { kind: "swarm_pending" as const, terminal: true },
      { kind: "swarm_pending" as const, terminal: true },
      { kind: "other" as const },
      { kind: "swarm_pending" as const, terminal: true },
    ];
    const rows = partitionActivityRows(items, (row) => ({
      isThinking: row.kind === "thinking",
      isTerminalSwarmPending: row.kind === "swarm_pending" && row.terminal,
    }));
    expect(rows.map((r) => r.kind)).toEqual([
      "thought",
      "swarms",
      "item",
      "item",
    ]);
    expect(rows[1]?.kind).toBe("swarms");
    if (rows[1]?.kind === "swarms") {
      expect(rows[1].items).toHaveLength(3);
    }
  });

  it("leaves a single terminal swarm_pending as its own pill", () => {
    const items = [{ kind: "swarm_pending" as const, terminal: true }];
    const rows = partitionActivityRows(items, (row) => ({
      isThinking: false,
      isTerminalSwarmPending: row.kind === "swarm_pending",
    }));
    expect(rows.map((r) => r.kind)).toEqual(["item"]);
  });

  it("coalesces consecutive Thought siblings into one fold", () => {
    const items = [
      { kind: "thinking" as const },
      { kind: "thinking" as const },
      { kind: "thinking" as const },
      { kind: "card" as const },
      { kind: "other" as const },
      { kind: "thinking" as const },
      { kind: "thinking" as const },
    ];
    const rows = partitionActivityRows(items, (row) => ({
      isThinking: row.kind === "thinking",
    }));
    expect(rows.map((r) => r.kind)).toEqual([
      "thought",
      "item",
      "item",
      "thought",
    ]);
    expect(rows[0]?.kind).toBe("thought");
    if (rows[0]?.kind === "thought") {
      expect(rows[0].items).toHaveLength(3);
    }
    expect(rows[3]?.kind).toBe("thought");
    if (rows[3]?.kind === "thought") {
      expect(rows[3].items).toHaveLength(2);
    }
  });
});

describe("live fold (Exploring + live line + flat rows)", () => {
  it("shows Exploring, a live line for the running tool, and never Working...", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "debug the redirect" } },
      {
        kind: "thinking",
        text: "Working...", // spoken-prose fallback must not become Thought chrome
        id: "th-live",
        streaming: true,
      },
      {
        kind: "card",
        card: {
          id: "c-live-1",
          goal: "git status",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "c-live-2",
          goal: "rg ActionForm",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];

    render(
      <TranscriptList
        {...listProps(items)}
        status="executing"
        turnOpen
      />,
    );

    const workChrome = screen.getByRole("button", { name: /Exploring ran 2 commands/i });
    expect(workChrome.textContent || "").not.toMatch(/Working\.\.\./i);
    expect(screen.queryByText("Working...")).toBeNull();
    expect(screen.getByTestId("live-activity-line").textContent).toBe("Running rg ActionForm");

    fireEvent.click(workChrome);

    // One flat row per step: no Ran N sub-fold.
    expect(screen.queryByTestId("ran-commands-fold")).toBeNull();
    const rows = screen.getByTestId("activity-rows");
    expect(within(rows).getByTestId("thought-fold").textContent).toMatch(/Thinking/);
    expect(within(rows).getAllByText("Running")).toHaveLength(2);
    expect(within(rows).getByText("git status")).toBeTruthy();
    expect(within(rows).getByText("rg ActionForm")).toBeTruthy();
  });
});

describe("sealed fold (Worked for + flat Thought / Ran rows)", () => {
  it("shows Worked for + finale Bubble; rows are flat; finale never inside fold", () => {
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
    expect(screen.queryByText(/Exploring/i)).toBeNull();

    // Finale stays a top-level Bubble — visible without expanding Worked for.
    const finale = screen.getByText(/The redirect was missing a return/i);
    expect(finale.closest(".transcript-msg-body")?.className).toMatch(/font-normal/);

    expect(screen.queryByTestId("thought-fold")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Worked for/i }));
    const rows = screen.getByTestId("activity-rows");
    expect(within(rows).getAllByTestId("thought-fold")).toHaveLength(2);
    expect(within(rows).getByText("Thought 8s")).toBeTruthy();
    expect(within(rows).getByText("Thought 2s")).toBeTruthy();
    expect(within(rows).getAllByText("Ran")).toHaveLength(2);
    expect(within(rows).getByText("git status")).toBeTruthy();
    expect(rows.contains(finale)).toBe(false);
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

describe("stacked Swarm done fold", () => {
  it("collapses eight terminal swarm pills behind Swarm done · 8", () => {
    const pendings: Item[] = Array.from({ length: 8 }, (_, i) => ({
      kind: "swarm_pending" as const,
      job_ids: [`local-swarm-call_${i}`],
      objective: `Audit the freshly fetched repo ${i}`,
      status: "done" as const,
      resolved: true,
    }));
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit the agents" } },
      {
        kind: "card",
        card: {
          id: "c-swarm",
          goal: "run swarm",
          cwd: null,
          kind: "run_swarm",
          running: true,
          open: false,
        },
      },
      ...pendings,
    ];

    render(
      <TranscriptList
        {...listProps(items)}
        status="executing"
        turnOpen
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Exploring/i }));

    const fold = screen.getByTestId("swarm-done-fold");
    expect(fold.querySelector(".transcript-fold-chrome")?.textContent || "").toMatch(/Swarm done · 8/);
    expect(screen.queryAllByText(/swarm done:/i)).toHaveLength(0);

    fireEvent.click(within(fold).getByRole("button"));
    expect(screen.getAllByText(/swarm done:/i).length).toBe(8);
  });
});
