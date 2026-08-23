import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";

afterEach(() => cleanup());

describe("TranscriptList log region", () => {
  it("exposes a polite log that announces additions, not every token", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "hello" } },
      { kind: "msg", msg: { role: "assistant", text: "hi" } },
    ];
    render(
      <TranscriptList
        items={items}
        status="idle"
        compactingStatus={null}
        editingIndex={null}
        auto={false}
        plan={false}
        scrollContainerRef={{ current: null }}
        onEditMessage={vi.fn()}
        onExecuteSend={vi.fn()}
        onImageClick={vi.fn()}
        onSetCard={vi.fn()}
        onExecutePlan={vi.fn()}
        onCommandApproval={vi.fn()}
      />,
    );
    const log = screen.getByTestId("transcript-log");
    expect(log.getAttribute("role")).toBe("log");
    expect(log.getAttribute("aria-live")).toBe("polite");
    expect(log.getAttribute("aria-relevant")).toBe("additions");
    expect(log.getAttribute("aria-atomic")).toBe("false");
  });
});
