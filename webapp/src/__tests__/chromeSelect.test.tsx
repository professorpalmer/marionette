import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";
import { TranscriptList, type Item } from "../components/TranscriptList";
import css from "../index.css?raw";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";

afterEach(() => cleanup());

const listProps = (items: Item[]) => ({
  items,
  status: "done" as const,
  compactingStatus: null,
  editingIndex: null,
  auto: false,
  plan: false,
  turnOpen: false,
  scrollContainerRef: createRef<HTMLDivElement>(),
  onEditMessage: vi.fn(),
  onExecuteSend: vi.fn(),
  onImageClick: vi.fn(),
  onSetCard: vi.fn(),
  onExecutePlan: vi.fn(),
  onCommandApproval: vi.fn(),
});

describe("feed selection chrome", () => {
  it("CSS keeps message body selectable and fold/virtual chrome unselectable", () => {
    expect(css).toMatch(/\.transcript-msg-body\s*\{[^}]*user-select:\s*text/);
    expect(css).toMatch(/\.transcript-msg-body\s*\{[^}]*font-weight:\s*400/);
    expect(css).toMatch(/\[data-testid="transcript-virtual-row"\][^{]*\{[^}]*user-select:\s*none/);
    expect(css).toMatch(/\.transcript-fold-chrome[^{]*\{[^}]*user-select:\s*none/);
    expect(column).toMatch(/data-testid="composer-chrome"/);
    expect(column).toMatch(/data-testid="jump-to-latest"/);
  });

  it("message body is select-text and virtual wrappers are select-none", () => {
    render(
      <TranscriptList {...listProps([{ kind: "msg", msg: { role: "user", text: "hello range" } }])} />,
    );
    const body = document.querySelector(".transcript-msg-body");
    expect(body).toBeTruthy();
    expect(body).toHaveClass("select-text");
    expect(screen.getByText("hello range")).toBeTruthy();
    const wrappers = document.querySelectorAll("[data-testid='transcript-virtual-row'], .transcript-virtual-row");
    expect(wrappers.length).toBeGreaterThan(0);
    wrappers.forEach((node) => {
      expect(node.className).toMatch(/select-none/);
    });
  });

  it("spoken assistant body is regular weight (not semibold/bold on the wrapper)", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "ask" } },
          {
            kind: "msg",
            msg: {
              role: "assistant",
              text: "Plain spoken answer with **emphasis** only where marked.",
            },
          },
        ])}
      />,
    );
    const bodies = document.querySelectorAll(".transcript-msg-body");
    expect(bodies.length).toBeGreaterThanOrEqual(2);
    const spoken = Array.from(bodies).find((el) =>
      (el.textContent || "").includes("Plain spoken answer"),
    );
    expect(spoken).toBeTruthy();
    expect(spoken!.className).toMatch(/font-normal/);
    expect(spoken!.className).not.toMatch(/font-semibold/);
    expect(spoken!.className).not.toMatch(/font-bold/);
    expect(spoken!.className).not.toMatch(/font-medium/);
    const para = spoken!.querySelector("p");
    expect(para).toBeTruthy();
    expect(para!.className).toMatch(/font-normal/);
    expect(para!.className).not.toMatch(/font-semibold/);
    expect(para!.className).not.toMatch(/font-bold/);
    const strong = spoken!.querySelector("strong");
    expect(strong).toBeTruthy();
    expect(strong!.className).toMatch(/font-semibold/);
  });
});
