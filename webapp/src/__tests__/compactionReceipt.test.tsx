import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";

function renderItems(items: Item[]) {
  render(
    <TranscriptList
      items={items}
      status="done"
      compactingStatus={null}
      editingIndex={null}
      auto
      plan={false}
      turnOpen={false}
      scrollContainerRef={{ current: null }}
      onEditMessage={vi.fn()}
      onExecuteSend={vi.fn()}
      onImageClick={vi.fn()}
      onSetCard={vi.fn()}
      onExecutePlan={vi.fn()}
    />,
  );
}

describe("compaction receipt label", () => {
  it("names tool-result aging and its count", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "compaction",
        before_tokens: 52000,
        after_tokens: 40000,
        mode: "tool_result_aging",
        aged_results: 3,
      },
    ]);
    expect(screen.getAllByText("Aged 3 old tool results").length).toBeGreaterThan(0);
    expect(screen.queryByText("Context compacted")).toBeNull();
  });

  it("keeps the generic label for a summary compaction", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "compaction", before_tokens: 9000, after_tokens: 3000, mode: "llm" },
    ]);
    expect(screen.getAllByText("Context compacted").length).toBeGreaterThan(0);
  });
});
