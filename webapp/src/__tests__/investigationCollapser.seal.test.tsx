import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  type Item,
} from "../components/TranscriptList";

afterEach(() => cleanup());

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

    expect(screen.getByText(/Explored/i)).toBeTruthy();
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

    // Prior fold must stay Explored / collapsed while busy with no turn-2 tools.
    expect(screen.getByText(/Explored/i)).toBeTruthy();
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
    expect(screen.getByText(/Explored/i)).toBeTruthy();
    // Prior fold still sealed (collapsed); only the live fold is active.
    expect(screen.queryByText(/looking at auth handlers/i)).toBeNull();
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

    // Pause-point: Explored fold + Still working footer (not Investigating spinner).
    expect(screen.getByText(/Explored/i)).toBeTruthy();
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
    expect(screen.getByText(/Explored/i)).toBeTruthy();
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
    // Investigating fold owns the status surface — no flat Still working footer.
    expect(screen.queryByText(/Still working/i)).toBeNull();
  });

  it("holdSwarmAwait with active pilot turn keeps mid-turn Investigating, not sealed Explored", () => {
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
    expect(screen.queryByText(/Explored/i)).toBeNull();

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
    expect(screen.queryByText(/Explored/i)).toBeNull();

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
    expect(screen.queryByText(/Explored/i)).toBeNull();
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

    expect(screen.getByText(/Explored/i)).toBeTruthy();
    expect(screen.queryByText(/Investigating/i)).toBeNull();
    // Busy footer owns Still working… (matches StatusPill), not sticky Investigating.
    expect(screen.getByText(/Still working/i)).toBeTruthy();
  });
});
