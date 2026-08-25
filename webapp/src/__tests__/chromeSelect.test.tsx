import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";
import { TranscriptList, type Item } from "../components/TranscriptList";
import css from "../index.css?raw";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";

afterEach(() => cleanup());

describe("feed selection chrome", () => {
  it("CSS keeps message body selectable and fold/virtual chrome unselectable", () => {
    expect(css).toMatch(/\.transcript-msg-body\s*\{[^}]*user-select:\s*text/);
    expect(css).toMatch(/\[data-testid="transcript-virtual-row"\][^{]*\{[^}]*user-select:\s*none/);
    expect(css).toMatch(/\.transcript-fold-chrome[^{]*\{[^}]*user-select:\s*none/);
    expect(column).toMatch(/data-testid="composer-chrome"/);
    expect(column).toMatch(/data-testid="jump-to-latest"/);
  });

  it("message body is select-text and virtual wrappers are select-none", () => {
    const items: Item[] = [{ kind: "msg", msg: { role: "user", text: "hello range" } }];
    render(
      <TranscriptList
        items={items}
        status="done"
        compactingStatus={null}
        editingIndex={null}
        auto={false}
        plan={false}
        turnOpen={false}
        scrollContainerRef={createRef<HTMLDivElement>()}
        onEditMessage={vi.fn()}
        onExecuteSend={vi.fn()}
        onImageClick={vi.fn()}
        onSetCard={vi.fn()}
        onExecutePlan={vi.fn()}
        onCommandApproval={vi.fn()}
      />,
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
});
