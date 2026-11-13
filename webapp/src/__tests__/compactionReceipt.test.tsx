import { fireEvent, render, screen } from "@testing-library/react";
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

describe("system event divider rows", () => {
  it("shows a compaction as one divider with token counts and a summary toggle", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "done" } },
      {
        kind: "compaction",
        before_tokens: 14259,
        after_tokens: 3721,
        mode: "extractive",
        kept: ["src/calc.py"],
        dropped: ["old.log"],
        handles: ["job_be98"],
      },
    ]);
    const row = screen.getByTestId("compaction-event");
    expect(row.textContent).toContain("Context compacted · 14.3k to 3.7k tokens");
    expect(screen.queryByTestId("compaction-summary")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View summary" }));
    expect(screen.getByTestId("compaction-summary").textContent).toContain("kept 1 · dropped 1");
    expect(screen.getByTestId("compaction-summary").textContent).toContain("job_be98");
  });

  it("shows an aborted compaction without token counts or a toggle", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "done" } },
      { kind: "compaction", before_tokens: 0, after_tokens: 0, aborted: true, message: "Not enough history to compact yet" },
    ]);
    expect(screen.getByTestId("compaction-event").textContent).toContain("Not enough history to compact yet");
    expect(screen.getByTestId("compaction-event").textContent).not.toContain("tokens");
    expect(screen.queryByRole("button", { name: "View summary" })).toBeNull();
  });

  it("marks a model switch between two turns", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "one", model: "openrouter:deepseek/deepseek-v4.1-flash" } },
      { kind: "msg", msg: { role: "assistant", text: "ok" } },
      { kind: "msg", msg: { role: "user", text: "two", model: "openrouter:deepseek/deepseek-v4.1-flash" } },
      { kind: "msg", msg: { role: "assistant", text: "ok" } },
      { kind: "msg", msg: { role: "user", text: "three", model: "openrouter:openai/gpt-6.1-sol" } },
    ]);
    const rows = screen.getAllByTestId("model-switch-event");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("Model switched · deepseek-v4.1-flash to gpt-6.1-sol");
  });
});
