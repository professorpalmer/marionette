import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  type Item,
} from "../components/TranscriptList";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function sealedCard(id: string, goal: string): Extract<Item, { kind: "card" }> {
  return {
    kind: "card",
    card: {
      id,
      goal,
      cwd: null,
      kind: "read_file",
      running: false,
      open: false,
      result: { status: "ok" },
    },
  };
}

function listProps(
  items: Item[],
  opts: {
    turnOpen: boolean;
    status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | "awaiting_swarm";
    holdSwarmAwait?: boolean;
    busyElapsedMs?: number | null;
  },
) {
  return {
    items,
    status: opts.status,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: opts.turnOpen,
    holdSwarmAwait: opts.holdSwarmAwait ?? false,
    busyElapsedMs: opts.busyElapsedMs ?? null,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

describe("prior investigation fold stays sealed on new prompt", () => {
  it("keeps a sealed prior fold collapsed and non-spinning until the new turn has tools", () => {
    const turn1: Item[] = [
      { kind: "msg", msg: { role: "user", text: "investigate auth" } },
      { kind: "thinking", text: "looking at auth handlers", id: "th-t1-seal" },
      sealedCard("card-t1-a", "auth.ts"),
      sealedCard("card-t1-b", "session.ts"),
      sealedCard("card-t1-c", "middleware.ts"),
      { kind: "msg", msg: { role: "assistant", text: "Auth looks fine." } },
    ];

    const { rerender } = render(
      <TranscriptList {...listProps(turn1, { turnOpen: false, status: "idle" })} />,
    );

    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    // Sealed fold starts collapsed — inner thinking is not mounted.
    expect(screen.queryByText(/looking at auth handlers/i)).toBeNull();

    const afterNewPrompt: Item[] = [
      ...turn1,
      { kind: "msg", msg: { role: "user", text: "now check billing" } },
    ];
    rerender(
      <TranscriptList
        {...listProps(afterNewPrompt, { turnOpen: true, status: "thinking" })}
      />,
    );

    // Prior fold must stay Worked for / collapsed while busy with no turn-2 tools.
    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    expect(screen.queryByText(/looking at auth handlers/i)).toBeNull();

    const withTurn2Tool: Item[] = [
      ...afterNewPrompt,
      { kind: "thinking", text: "billing next", id: "th-t2-live" },
      {
        kind: "card",
        card: {
          id: "card-t2-a",
          goal: "billing.ts",
          cwd: null,
          kind: "read_file",
          running: true,
          open: false,
        },
      },
    ];
    rerender(
      <TranscriptList
        {...listProps(withTurn2Tool, { turnOpen: true, status: "executing" })}
      />,
    );

    expect(screen.getByText(/Investigating/i)).toBeTruthy();
    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    // Prior fold still sealed (collapsed); only the live fold is active.
    expect(screen.queryByText(/looking at auth handlers/i)).toBeNull();
  });

  it("keeps Worked for wall-clock on a prior fold when the next prompt starts", () => {
    const turn1: Item[] = [
      { kind: "msg", msg: { role: "user", text: "Hello, Bonsai!" } },
      { kind: "thinking", text: "greeting", id: "th-bonsai-1" },
      { kind: "msg", msg: { role: "assistant", text: "Hey! Bonsai here." } },
    ];
    const wall = 6 * 60_000 + 22_000;

    const { rerender } = render(
      <TranscriptList
        {...listProps(turn1, {
          turnOpen: false,
          status: "idle",
          busyElapsedMs: wall,
        })}
      />,
    );
    expect(screen.getByRole("button", { name: /Worked for 6m 22s/i })).toBeTruthy();

    rerender(
      <TranscriptList
        {...listProps(
          [...turn1, { kind: "msg", msg: { role: "user", text: "What is a hashmap?" } }],
          { turnOpen: true, status: "thinking", busyElapsedMs: 1000 },
        )}
      />,
    );
    expect(screen.getByRole("button", { name: /Worked for 6m 22s/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Worked for 1s/i })).toBeNull();
  });

  it("does not let an early thinking slice replace the remembered wall-clock", () => {
    const turn1: Item[] = [
      { kind: "msg", msg: { role: "user", text: "What is a hashmap?" } },
      { kind: "thinking", text: "hashmap plan", id: "th-bonsai-2", duration_ms: 1000 },
      { kind: "msg", msg: { role: "assistant", text: "A hashmap stores pairs." } },
    ];
    const wall = 6 * 60_000 + 22_000;

    const { rerender } = render(
      <TranscriptList
        {...listProps(turn1, {
          turnOpen: false,
          status: "idle",
          busyElapsedMs: wall,
        })}
      />,
    );
    expect(screen.getByRole("button", { name: /Worked for 6m 22s/i })).toBeTruthy();

    rerender(
      <TranscriptList
        {...listProps(
          [...turn1, { kind: "msg", msg: { role: "user", text: "how many R's?" } }],
          { turnOpen: true, status: "thinking", busyElapsedMs: 1000 },
        )}
      />,
    );
    expect(screen.getByRole("button", { name: /Worked for 6m 22s/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Worked for 1s/i })).toBeNull();
  });
});

describe("holdSwarmAwait transcript latch + awaiting_swarm pause-point", () => {
  const pauseItems: Item[] = [
    { kind: "msg", msg: { role: "user", text: "dispatch workers" } },
    { kind: "thinking", text: "spawning swarm", id: "th-pause" },
    sealedCard("card-pause-a", "auth.ts"),
    sealedCard("card-pause-b", "billing.ts"),
    {
      kind: "msg",
      msg: { role: "assistant", text: "Workers flying — validating when they land." },
    },
    {
      kind: "swarm_pending",
      job_ids: ["job_abcdef012345"],
      objective: "audit auth",
      status: "running",
    },
  ];

  it("holdSwarmAwait keeps absorption latch through idle/thinking status flaps", () => {
    const { rerender } = render(
      <TranscriptList
        {...listProps(pauseItems, {
          turnOpen: false,
          status: "awaiting_swarm",
          holdSwarmAwait: true,
        })}
      />,
    );

    // Pause-point: Worked for fold + Still working footer (not Investigating spinner).
    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    expect(screen.getByText(/Still working/i)).toBeTruthy();

    // Idle flap: without hold, agentLoopOpen would drop; with hold, latch + footer stay.
    rerender(
      <TranscriptList
        {...listProps(pauseItems, {
          turnOpen: false,
          status: "idle",
          holdSwarmAwait: true,
        })}
      />,
    );
    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    expect(screen.getByText(/Still working/i)).toBeTruthy();

    // Pilot busy (thinking): holdSwarmAwait must not seal — live swarm keeps Investigating.
    rerender(
      <TranscriptList
        {...listProps(pauseItems, {
          turnOpen: false,
          status: "thinking",
          holdSwarmAwait: true,
        })}
      />,
    );
    expect(screen.getByText(/Investigating/i)).toBeTruthy();
    // Finished cards + open loop: fold stays Investigating and the footer
    // keeps Still working… so tool-batch gaps are not a dead log dump.
    expect(screen.getByText(/Still working/i)).toBeTruthy();
    // Spoken assistant prose stays a top-level Bubble after the fold.
    expect(screen.getByText(/Workers flying — validating when they land/i)).toBeTruthy();
  });

  it("keeps Worked for on the same busy clock as Still working while a swarm holds", () => {
    const shortSlice: Item[] = [
      { kind: "msg", msg: { role: "user", text: "can u pick back up? use astra workers" } },
      {
        kind: "thinking",
        text: "resuming",
        id: "th-resume",
        duration_ms: 2_000,
      },
      {
        kind: "card",
        card: {
          id: "card-resume",
          goal: "continue export",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { status: "ok", duration_ms: 5_000 },
        },
      },
      {
        kind: "swarm_result",
        job_id: "job_resume",
        applied: false,
        files: [],
        summary: "workers flying",
        error: null,
      },
    ];
    render(
      <TranscriptList
        {...listProps(shortSlice, {
          turnOpen: false,
          status: "awaiting_swarm",
          holdSwarmAwait: true,
          busyElapsedMs: 11 * 60_000 + 22_000,
        })}
      />,
    );
    expect(screen.getByRole("button", { name: /Worked for 11m 22s/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Worked for 7s/i })).toBeNull();
    expect(screen.getByText(/Still working/i)).toBeTruthy();
  });

  it("holdSwarmAwait with active pilot turn keeps mid-turn Investigating, not sealed Worked for", () => {
    const midTurnItems: Item[] = [
      { kind: "msg", msg: { role: "user", text: "check auth while workers run" } },
      { kind: "thinking", text: "reading auth handlers", id: "th-mid" },
      sealedCard("card-mid-a", "auth.ts"),
      {
        kind: "card",
        card: {
          id: "card-mid-live",
          goal: "session.ts",
          cwd: null,
          kind: "read_file",
          running: true,
          open: false,
        },
      },
    ];

    const { rerender } = render(
      <TranscriptList
        {...listProps(midTurnItems, {
          turnOpen: true,
          status: "executing",
          holdSwarmAwait: true,
        })}
      />,
    );

    expect(screen.getByText(/Investigating/i)).toBeTruthy();
    expect(screen.queryByText(/Worked for/i)).toBeNull();
    // Running tool used to swallow the under-fold timer line.
    expect(screen.getByText(/step /i)).toBeTruthy();

    rerender(
      <TranscriptList
        {...listProps(midTurnItems, {
          turnOpen: true,
          status: "thinking",
          holdSwarmAwait: true,
        })}
      />,
    );

    expect(screen.getByText(/Investigating/i)).toBeTruthy();
    expect(screen.queryByText(/Worked for/i)).toBeNull();
    expect(screen.getByText(/step /i)).toBeTruthy();

    // Settled tools but pilot still busy — must not seal via hold alone.
    const settledMidTurn: Item[] = [
      ...midTurnItems.slice(0, -1),
      sealedCard("card-mid-live", "session.ts"),
    ];
    rerender(
      <TranscriptList
        {...listProps(settledMidTurn, {
          turnOpen: true,
          status: "thinking",
          holdSwarmAwait: true,
        })}
      />,
    );
    expect(screen.getByText(/Investigating/i)).toBeTruthy();
    expect(screen.queryByText(/Worked for/i)).toBeNull();
    // Finished cards, loop still open: Still working… · step N stays painted.
    expect(screen.getByText(/Still working/i)).toBeTruthy();
  });

  it("awaiting_swarm pause-point does not keep Investigating spinner over settled tools", () => {
    render(
      <TranscriptList
        {...listProps(pauseItems, {
          turnOpen: false,
          status: "awaiting_swarm",
          holdSwarmAwait: false,
        })}
      />,
    );

    expect(screen.getByText(/Worked for/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    // Busy footer owns Still working… (matches StatusPill), not sticky Investigating.
    expect(screen.getByText(/Still working/i)).toBeTruthy();
  });
});

describe("prior fold does not stay Investigating after steer flush", () => {
  function runningCard(id: string, goal: string): Extract<Item, { kind: "card" }> {
    return {
      kind: "card",
      card: {
        id,
        goal,
        cwd: null,
        kind: "read_file",
        running: true,
        open: false,
      },
    };
  }

  it("stale running / swarm_pending in the prior fold stay Worked for while the live fold investigates", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "do the work" } },
      runningCard("stale-card", "auth.ts"),
      {
        kind: "swarm_pending",
        job_ids: ["job_stale"],
        objective: "audit auth",
        status: "running",
      },
      { kind: "steer", text: "also check billing" },
      runningCard("live-card", "billing.ts"),
    ];

    render(
      <TranscriptList
        {...listProps(items, { turnOpen: true, status: "executing" })}
      />,
    );

    const investigating = screen.getAllByText(/Investigating/i);
    const worked = screen.getAllByText(/Worked for/i);
    expect(investigating).toHaveLength(1);
    expect(worked).toHaveLength(1);
  });

  it("sealed prior cards never spin even when a later fold is live", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "do the work" } },
      sealedCard("sealed-a", "auth.ts"),
      sealedCard("sealed-b", "session.ts"),
      { kind: "steer", text: "also check billing" },
      runningCard("live-card", "billing.ts"),
    ];

    render(
      <TranscriptList
        {...listProps(items, { turnOpen: true, status: "executing" })}
      />,
    );

    expect(screen.getAllByText(/Investigating/i)).toHaveLength(1);
    expect(screen.getAllByText(/Worked for/i)).toHaveLength(1);
  });

  it("prior-fold durable job shows quiet job still running, not a second Investigating", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "do the work" } },
      {
        kind: "card",
        card: {
          id: "durable-prior",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
          result: { job_id: "local-cmd-1", status: "pending" },
        },
      },
      { kind: "steer", text: "also check billing" },
      runningCard("live-card", "billing.ts"),
    ];

    render(
      <TranscriptList
        {...listProps(items, { turnOpen: true, status: "executing" })}
      />,
    );

    expect(screen.getAllByText(/Investigating/i)).toHaveLength(1);
    expect(screen.getByText(/job still running/i)).toBeTruthy();
    expect(screen.queryByText(/Worked for/i)).toBeNull();
  });

  it("prior-fold swarm_pending only shows Swarm pending, not a second Investigating", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "do the work" } },
      {
        kind: "swarm_pending",
        job_ids: ["job_stale"],
        objective: "audit auth",
        status: "running",
      },
      { kind: "steer", text: "also check billing" },
      runningCard("live-card", "billing.ts"),
    ];

    render(
      <TranscriptList
        {...listProps(items, { turnOpen: true, status: "executing" })}
      />,
    );

    expect(screen.getAllByText(/Investigating/i)).toHaveLength(1);
    expect(screen.getByText(/Swarm · 1 pending/i)).toBeTruthy();
    expect(screen.queryByText(/Worked for/i)).toBeNull();
  });

  it("holdSwarmAwait cannot keep a prior swarm_pending fold Investigating", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "do the work" } },
      {
        kind: "swarm_pending",
        job_ids: ["job_stale"],
        objective: "audit auth",
        status: "running",
      },
      { kind: "steer", text: "also check billing" },
      runningCard("live-card", "billing.ts"),
    ];

    render(
      <TranscriptList
        {...listProps(items, {
          turnOpen: true,
          status: "awaiting_swarm",
          holdSwarmAwait: true,
        })}
      />,
    );

    expect(screen.getAllByText(/Investigating/i)).toHaveLength(1);
    expect(screen.getByText(/Swarm · 1 pending/i)).toBeTruthy();
  });

  it("does not paint the same reasoning paragraph twice in one Thought fold", () => {
    const phrase =
      "The user is greeting me again. This is a simple greeting — no tool calls needed. I'll respond briefly and warmly.";
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "Hey, Bonsai!" } },
      { kind: "thinking", text: phrase, id: "th-live" },
      { kind: "msg", msg: { role: "assistant", text: "Hey! What's on your mind?" } },
      { kind: "thinking", text: phrase, id: "th-replay" },
    ];

    render(
      <TranscriptList {...listProps(items, { turnOpen: false, status: "idle" })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Worked for/i }));
    fireEvent.click(screen.getByRole("button", { name: /Thought/i }));
    expect(screen.getAllByText(phrase)).toHaveLength(1);
  });
});
