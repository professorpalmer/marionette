import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";
import { _resetAgentCommandIndexForTests, registerAgentCommandSession } from "../lib/agentCommandIndex";

afterEach(() => {
  cleanup();
  _resetAgentCommandIndexForTests();
});

function props(items: Item[]) {
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

it("a new command session does not re-parse or remount settled answers", () => {
  const items: Item[] = [
    { kind: "msg", msg: { id: "u0", role: "user", text: "status?" } },
    { kind: "msg", msg: { id: "a0", role: "assistant", text: "Run the check.\n\n```ts\nconst a = 1;\n```\n\nThen `git status` again." } },
  ];
  const { container } = render(<TranscriptList {...props(items)} />);
  const para = container.querySelector("p");
  const fenced = container.querySelector("code.language-ts, code[class*=language-]");
  expect(para && fenced).toBeTruthy();
  act(() => {
    registerAgentCommandSession({ id: "cmd-1", command: "git status", sessionId: "s1" });
  });
  // Same nodes: selection, the code block's Copy state and images survive.
  expect(container.querySelector("p")).toBe(para);
  expect(container.querySelector("code.language-ts, code[class*=language-]")).toBe(fenced);
  // The inline command itself did pick up the new session.
  expect(container.querySelector('button[title="Reveal running command"]')).toBeTruthy();
});
