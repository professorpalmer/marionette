import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const registered = vi.hoisted(() => ({ calls: 0 }));
vi.mock("../lib/agentCommandIndex", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/agentCommandIndex")>();
  return {
    ...actual,
    registerAgentCommandSession: (input: Parameters<typeof actual.registerAgentCommandSession>[0]) => {
      registered.calls += 1;
      return actual.registerAgentCommandSession(input);
    },
  };
});
import { TranscriptList, type Item } from "../components/TranscriptList";
import {
  _resetAgentCommandIndexForTests,
  dismissAgentCommandSession,
  lookupAgentCommandSessionById,
  subscribeAgentCommandIndex,
} from "../lib/agentCommandIndex";

afterEach(() => {
  cleanup();
  _resetAgentCommandIndexForTests();
});

function props(items: Item[]) {
  return {
    items, status: "streaming" as const, compactingStatus: null as string | null,
    editingIndex: null as number | null, auto: false, plan: false, turnOpen: true,
    scrollContainerRef: { current: null }, sessionId: "s1",
    onEditMessage: vi.fn(), onExecuteSend: vi.fn(), onImageClick: vi.fn(),
    onSetCard: vi.fn(), onExecutePlan: vi.fn(), onCommandApproval: vi.fn(),
  };
}

it("stream frames do not re-index command cards that did not change", () => {
  const card: Item = {
    kind: "card",
    card: { id: "c1", kind: "run_command", goal: "git status", running: false, result: { status: "ok", command: "git status", output: "clean" } },
  } as Item;
  const user: Item = { kind: "msg", msg: { id: "u0", role: "user", text: "check" } };
  const frame = (text: string): Item[] => [user, card, { kind: "msg", msg: { id: "a0", role: "assistant", text, streaming: true } }];
  const { rerender } = render(<TranscriptList {...props(frame("W"))} />);
  expect(lookupAgentCommandSessionById("c1")).toBeTruthy();
  const before = registered.calls;
  let notifications = 0;
  const unsubscribe = subscribeAgentCommandIndex(() => { notifications += 1; });
  for (let i = 0; i < 10; i += 1) rerender(<TranscriptList {...props(frame(`W${"o".repeat(i)}rking`))} />);
  // Frames that only grow the answer do not re-classify or re-register the card.
  expect(registered.calls - before).toBe(0);
  expect(notifications).toBe(0);
  // An evicted/dismissed entry comes back on the next render.
  dismissAgentCommandSession("c1");
  rerender(<TranscriptList {...props(frame("Done"))} />);
  expect(lookupAgentCommandSessionById("c1")).toBeTruthy();
  unsubscribe();
});
