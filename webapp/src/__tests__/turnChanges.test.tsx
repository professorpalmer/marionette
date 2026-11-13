import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const restoreCheckpoint = vi.fn();
vi.mock("../lib/api", async (orig) => {
  const real = await orig<typeof import("../lib/api")>();
  return { ...real, api: { ...real.api, restoreCheckpoint: (id: string) => restoreCheckpoint(id) } };
});

import { clearActivityFoldPrefs, TranscriptList, type Item } from "../components/TranscriptList";

function editCard(id: string, path: string, added: number, removed: number, open = false): Item {
  return {
    kind: "card",
    card: {
      id,
      kind: "edit_file",
      goal: path,
      cwd: null,
      running: false,
      open,
      result: {
        status: "ok",
        diff: {
          added,
          removed,
          patch: `--- a/${path}\n+++ b/${path}\n@@ -1,2 +1,2 @@\n-old line\n+new line\n context\n`,
          truncated: false,
        },
      },
    },
  };
}

function turn(prompt: string, cp: string, cards: Item[]): Item[] {
  return [
    { kind: "msg", msg: { role: "user", text: prompt } },
    { kind: "checkpoint", id: cp, label: "Before editing", trigger: "edit_file" },
    ...cards,
    { kind: "msg", msg: { role: "assistant", text: "Done." } },
  ];
}

function renderItems(items: Item[], turnOpen = false) {
  return render(
    <TranscriptList
      items={items}
      status={turnOpen ? "streaming" : "done"}
      compactingStatus={null}
      editingIndex={null}
      auto
      plan={false}
      turnOpen={turnOpen}
      scrollContainerRef={{ current: null }}
      onEditMessage={vi.fn()}
      onExecuteSend={vi.fn()}
      onImageClick={vi.fn()}
      onSetCard={vi.fn()}
      onExecutePlan={vi.fn()}
    />,
  );
}

beforeEach(() => restoreCheckpoint.mockReset());

describe("edit row diff", () => {
  it("shows the line counts on the row and the patch when open", () => {
    renderItems(turn("fix it", "cp1", [editCard("e1", "src/calc.py", 3, 1, true)]));
    fireEvent.click(screen.getByRole("button", { name: /Worked for/ }));
    const counts = screen.getAllByTestId("edit-diff-counts")[0];
    expect(counts.textContent).toBe("+3-1");
    const patch = screen.getByTestId("edit-diff-patch");
    expect(patch.textContent).toContain("+new line");
    expect(within(patch).getByText("-old line").className).toContain("text-risk");
    expect(within(patch).getByText("+new line").className).toContain("text-good");
  });
});

describe("edit row body", () => {
  it("shows the diff, not the internal fields", () => {
    // Rig K19: the open row showed path, cwd, "found 1 artifacts · file"
    // and "FILE edited src/calc.py: replaced 16 chars -> 16 chars".
    clearActivityFoldPrefs();
    const card = editCard("e1", "src/calc.py", 1, 1, true);
    if (card.kind !== "card") throw new Error("card");
    card.card.cwd = "/work/proj";
    card.card.result = {
      ...card.card.result,
      num: 1,
      types: ["file"],
      artifacts: [{ type: "file", headline: "edited src/calc.py: replaced 16 chars -> 16 chars" }],
    };
    renderItems(turn("fix it", "cp1", [card]));
    fireEvent.click(screen.getByRole("button", { name: /Worked for/ }));
    const body = screen.getByTestId("edit-diff-patch").parentElement!;
    expect(body.textContent).toContain("+new line");
    expect(body.textContent).not.toContain("/work/proj");
    expect(body.textContent).not.toContain("artifacts");
    expect(body.textContent).not.toContain("replaced 16 chars");
  });
});

describe("turn changed-files footer", () => {
  it("sums the turn's writes per file", () => {
    renderItems(turn("fix it", "cp1", [
      editCard("e1", "src/calc.py", 3, 1),
      editCard("e2", "src/calc.py", 2, 0),
      editCard("e3", "test_calc.py", 5, 0),
    ]));
    const footer = screen.getByTestId("turn-changes");
    expect(footer.textContent).toContain("2 files changed");
    expect(within(footer).getAllByTestId("edit-diff-counts")[0].textContent).toBe("+10-1");
    fireEvent.click(within(footer).getByTestId("turn-changes-review"));
    const files = within(footer).getByTestId("turn-changes-files");
    expect(files.textContent).toContain("src/calc.py+5-1");
    expect(files.textContent).toContain("test_calc.py+5");
  });

  it("Review shows the diff of each file, not only its name", () => {
    // Rig K18: Review listed names, and a click opened the whole file.
    renderItems(turn("fix it", "cp1", [
      editCard("e1", "src/calc.py", 1, 1),
      editCard("e2", "src/calc.py", 1, 1),
      editCard("e3", "test_calc.py", 1, 1),
    ]));
    const footer = screen.getByTestId("turn-changes");
    fireEvent.click(within(footer).getByTestId("turn-changes-review"));
    const patches = within(footer).getAllByTestId("edit-diff-patch");
    // One patch block per write, under its file.
    expect(patches).toHaveLength(3);
    expect(patches[0].textContent).toContain("+++ b/src/calc.py");
    expect(patches[2].textContent).toContain("+++ b/test_calc.py");
    expect(within(patches[0]).getByText("+new line").className).toContain("text-good");
  });

  it("has no footer for a turn without writes", () => {
    renderItems([
      { kind: "msg", msg: { role: "user", text: "hi" } },
      { kind: "msg", msg: { role: "assistant", text: "Hello." } },
    ]);
    expect(screen.queryByTestId("turn-changes")).toBeNull();
  });

  it("offers Undo only on the newest turn, and Undo then Redo restore", async () => {
    restoreCheckpoint
      .mockResolvedValueOnce({ ok: true, restored_files: ["a.py"], auto_snapshot_id: "auto9" })
      .mockResolvedValueOnce({ ok: true, restored_files: ["a.py"], auto_snapshot_id: "auto10" });
    renderItems([
      ...turn("one", "cp1", [editCard("e1", "a.py", 1, 0)]),
      ...turn("two", "cp2", [editCard("e2", "b.py", 1, 0)]),
    ]);
    const footers = screen.getAllByTestId("turn-changes");
    expect(footers).toHaveLength(2);
    expect(within(footers[0]).queryByTestId("turn-changes-undo")).toBeNull();
    await act(async () => {
      fireEvent.click(within(footers[1]).getByTestId("turn-changes-undo"));
    });
    expect(restoreCheckpoint).toHaveBeenCalledWith("cp2");
    expect(within(footers[1]).getByTestId("turn-changes-undone")).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(footers[1]).getByTestId("turn-changes-redo"));
    });
    expect(restoreCheckpoint).toHaveBeenLastCalledWith("auto9");
    expect(within(footers[1]).getByTestId("turn-changes-undo")).toBeTruthy();
  });

  it("shows a failed restore", async () => {
    restoreCheckpoint.mockRejectedValueOnce(new Error("Checkpoint cp1 not found in Git"));
    renderItems(turn("fix it", "cp1", [editCard("e1", "a.py", 1, 0)]));
    await act(async () => {
      fireEvent.click(screen.getByTestId("turn-changes-undo"));
    });
    expect(screen.getByTestId("turn-changes").textContent).toContain("Checkpoint cp1 not found in Git");
  });

  it("has no Undo while a turn is open", () => {
    renderItems([
      ...turn("one", "cp1", [editCard("e1", "a.py", 1, 0)]),
      { kind: "msg", msg: { role: "user", text: "two" } },
    ], true);
    expect(screen.getByTestId("turn-changes")).toBeTruthy();
    expect(screen.queryByTestId("turn-changes-undo")).toBeNull();
  });
});
