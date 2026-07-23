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
    status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming";
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
