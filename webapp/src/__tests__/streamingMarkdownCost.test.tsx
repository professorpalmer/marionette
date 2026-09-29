import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// Counts <code> elements the highlighter is handed: the real work it redoes.
const highlight = vi.hoisted(() => ({ codeNodes: 0 }));
vi.mock("rehype-highlight", () => ({
  default: () => (tree: unknown) => {
    const walk = (node: { tagName?: string; children?: unknown[] }) => {
      if (node.tagName === "code") highlight.codeNodes += 1;
      (node.children ?? []).forEach((child) => walk(child as typeof node));
    };
    walk(tree as { children?: unknown[] });
  },
}));

import { TranscriptList, type Item } from "../components/TranscriptList";

afterEach(cleanup);

function props(items: Item[]) {
  return {
    items,
    status: "streaming" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: true,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

function items(text: string): Item[] {
  return [
    // Conversation.setItems stamps durable ids; the live row key rides on them.
    { kind: "msg", msg: { id: "m-user-0", role: "user", text: "explain" } },
    { kind: "msg", msg: { id: "m-assistant-0", role: "assistant", text, streaming: true } },
  ];
}

it("streaming prose after a finished code block does not re-highlight that block", () => {
  let text = "Intro.\n\n```ts\nconst a = 1;\n```\n\nThen";
  const { rerender } = render(<TranscriptList {...props(items(text))} />);
  const before = highlight.codeNodes;
  for (let i = 0; i < 30; i += 1) {
    text += " word";
    rerender(<TranscriptList {...props(items(text))} />);
  }
  expect(screen.getByText(/Then( word)+/)).toBeTruthy();
  // Only the growing paragraph re-parses; the closed block is a memo hit.
  expect(highlight.codeNodes - before).toBe(0);
  expect(screen.getByText("const a = 1;")).toBeTruthy();
});
