import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  normalizeReasoningPreview,
  type Item,
} from "../components/TranscriptList";

afterEach(() => cleanup());

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

describe("normalizeReasoningPreview", () => {
  it("strips markdown emphasis markers from the first line", () => {
    expect(normalizeReasoningPreview("**Plan:** check `auth.ts` next\nmore")).toBe(
      "Plan: check auth.ts next",
    );
    expect(normalizeReasoningPreview("*Investigating* __handlers__")).toBe(
      "Investigating handlers",
    );
    // snake_case paths must survive (no single-underscore emphasis strip).
    expect(normalizeReasoningPreview("open auth_handlers.ts")).toBe(
      "open auth_handlers.ts",
    );
  });

  it("preserves ordinary asterisk math/glob text and strips links/strike", () => {
    expect(normalizeReasoningPreview("compute 2*3*4 next")).toBe("compute 2*3*4 next");
    expect(normalizeReasoningPreview("a*b*c")).toBe("a*b*c");
    expect(normalizeReasoningPreview("see [auth.ts](./auth.ts) and ~~old~~")).toBe(
      "see auth.ts and old",
    );
    expect(normalizeReasoningPreview("![diagram](./diag.png) overview")).toBe(
      "diagram overview",
    );
  });

  it("bounds length and ignores later lines", () => {
    const long = `${"a".repeat(200)}\nsecond line`;
    expect(normalizeReasoningPreview(long, 40)).toBe("a".repeat(40));
    expect(normalizeReasoningPreview("first\n**second**")).toBe("first");
  });
});

describe("transcript presentation contract", () => {
  it("keeps collapsed reasoning sentence-case sans without mono/uppercase/bold chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "look at auth" } },
          {
            kind: "thinking",
            text: "**Plan:** scan auth handlers",
            id: "th-present-1",
          },
        ])}
      />,
    );

    // Reasoning-only turns fold into a quiet activity summary; open it to
    // assert the inner Thought row presentation contract.
    fireEvent.click(screen.getByRole("button", { name: /Plan: scan auth handlers/i }));
    const thought = screen.getByRole("button", { name: /Thought/i });
    const classes = thought.className;
    expect(classes).not.toMatch(/uppercase/);
    expect(classes).not.toMatch(/font-mono/);
    expect(classes).not.toMatch(/tracking-wide/);
    expect(classes).toMatch(/font-sans/);
    expect(classes).toMatch(/font-normal/);
    expect(thought.textContent || "").not.toMatch(/\*\*/);
    expect(within(thought).getByText(/Plan: scan auth handlers/i)).toBeTruthy();
    expect(screen.queryByText(/REASONING/i)).toBeNull();
  });

  it("renders a quiet activity summary row without bordered pill chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "explore" } },
          {
            kind: "thinking",
            text: "mapping files",
            id: "th-act-1",
          },
          {
            kind: "card",
            card: {
              id: "c1",
              goal: "auth.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          {
            kind: "card",
            card: {
              id: "c2",
              goal: "session.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          {
            kind: "codegraph_context",
            symbols: 3,
            query: "auth",
          },
        ])}
      />,
    );

    const summary = screen.getByRole("button", { name: /Explored/i });
    expect(summary.className).not.toMatch(/rounded-lg/);
    expect(summary.className).not.toMatch(/border-edge/);
    expect(summary.className).not.toMatch(/bg-panel2/);
    expect(summary.className).toMatch(/font-sans/);
    expect(summary.getAttribute("aria-expanded")).toBe("false");
    // Secondary CodeGraph badge stays muted and does not dominate the label.
    const cg = within(summary).getByText(/\+ CodeGraph/);
    expect(cg.className).toMatch(/text-faint/);
  });

  it("keeps closed tool rows compact sans normal-weight with aria-expanded", () => {
    const onSetCard = vi.fn();
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "inspect" } },
          {
            kind: "card",
            card: {
              id: "tool-1",
              goal: "auth.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
        ])}
        onSetCard={onSetCard}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    // Exactly one keyboard disclosure control per closed tool row.
    const toolDisclosures = screen
      .getAllByRole("button", { expanded: false })
      .filter((el) => /^Read\b/i.test((el.textContent || "").trim()));
    expect(toolDisclosures).toHaveLength(1);
    const toolToggle = toolDisclosures[0];
    expect(toolToggle.className).not.toMatch(/font-mono/);
    expect(toolToggle.className).not.toMatch(/font-medium/);
    expect(toolToggle.className).toMatch(/font-sans/);
    expect(toolToggle.className).toMatch(/font-normal/);
    expect(toolToggle.getAttribute("aria-expanded")).toBe("false");
    // Target link remains a sibling control (not nested inside the expand button).
    expect(screen.getByRole("button", { name: /auth\.ts/i })).toBeTruthy();

    fireEvent.click(toolToggle);
    expect(onSetCard).toHaveBeenCalled();
  });

  it("preserves chronological user → thinking → tools → answer order", () => {
    const { container } = render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "check billing" } },
          { kind: "thinking", text: "billing next", id: "th-order" },
          {
            kind: "card",
            card: {
              id: "card-order",
              goal: "billing.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          { kind: "msg", msg: { role: "assistant", text: "Billing looks fine." } },
        ])}
      />,
    );

    const text = container.textContent || "";
    const userAt = text.indexOf("check billing");
    const exploredAt = text.search(/Explored/i);
    const answerAt = text.indexOf("Billing looks fine.");
    expect(userAt).toBeGreaterThanOrEqual(0);
    expect(exploredAt).toBeGreaterThan(userAt);
    expect(answerAt).toBeGreaterThan(exploredAt);
  });
});
