import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";

function renderCommand(command: string) {
  const items: Item[] = [
    { kind: "msg", msg: { role: "user", text: "clean up" } },
    {
      kind: "card",
      card: {
        id: `card-${command}`,
        kind: "run_command",
        goal: command,
        cwd: null,
        running: false,
        open: false,
        result: { status: "ok", output: "", duration_ms: 110 },
      },
    },
    { kind: "msg", msg: { role: "assistant", text: "Done." } },
  ];
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
  fireEvent.click(screen.getByRole("button", { name: /Worked for/ }));
  return screen.getByTestId("command-run");
}

function runEvents(spy: ReturnType<typeof vi.spyOn>) {
  return spy.mock.calls.filter((c) => (c[0] as Event).type === "harness-run-command").length;
}

describe("command row Run button", () => {
  it("runs a read command on one click", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const run = renderCommand("git status --porcelain");
    expect(run).toHaveAttribute("data-risk", "read");
    fireEvent.click(run);
    expect(runEvents(spy)).toBe(1);
    spy.mockRestore();
  });

  it("asks for a confirm before it runs a destructive command", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const run = renderCommand("rm -rf .pytest_cache && git status --porcelain");
    expect(run).toHaveAttribute("data-risk", "write");
    expect(run.textContent).toBe("Run…");
    fireEvent.click(run);
    expect(runEvents(spy)).toBe(0);
    expect(run.textContent).toBe("Confirm run");
    fireEvent.click(run);
    expect(runEvents(spy)).toBe(1);
    expect(run.textContent).toBe("Run…");
    spy.mockRestore();
  });
});
