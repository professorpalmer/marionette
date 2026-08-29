import { describe, expect, it } from "vitest";
import {
  collapseTodoTasks,
  litTodoContents,
  liveJobTodoLabels,
  todoHasWork,
  todoMatchesAnyDescription,
  todoPhaseProgress,
  todoSnapshotProgress,
  toRoman,
} from "../lib/composerTodos";
import type { Job } from "../lib/api";
import type { SessionTodoItem, SessionTodoSnapshot } from "../lib/api";

const task = (content: string, status: SessionTodoItem["status"]): SessionTodoItem => ({
  content,
  status,
});

describe("composerTodos", () => {
  it("rolls up phase and snapshot progress", () => {
    const snapshot: SessionTodoSnapshot = {
      phases: [
        { name: "WP-02", tasks: [task("a", "completed"), task("b", "in_progress"), task("c", "pending")] },
        { name: "WP-03", tasks: [task("d", "pending")] },
      ],
    };
    expect(todoPhaseProgress(snapshot.phases[0])).toEqual({ done: 1, total: 3 });
    expect(todoSnapshotProgress(snapshot)).toEqual({ done: 1, total: 4 });
    expect(todoHasWork(snapshot)).toBe(true);
    expect(todoHasWork({ phases: [] })).toBe(false);
  });

  it("folds overflow the way OMP does: two closed + five open", () => {
    const tasks = [
      task("old-1", "completed"),
      task("old-2", "completed"),
      task("old-3", "completed"),
      ...Array.from({ length: 7 }, (_, i) => task(`open-${i + 1}`, i === 0 ? "in_progress" : "pending")),
    ];
    const { items, hidden } = collapseTodoTasks(tasks);
    expect(items.map((row) => row.content)).toEqual([
      "old-2",
      "old-3",
      "open-1",
      "open-2",
      "open-3",
      "open-4",
      "open-5",
    ]);
    expect(hidden).toBe(3);
  });

  it("formats roman phase indexes", () => {
    expect(toRoman(1)).toBe("I");
    expect(toRoman(2)).toBe("II");
    expect(toRoman(4)).toBe("IV");
  });

  it("fuzzy-matches live job labels at six-character overlap", () => {
    expect(todoMatchesAnyDescription("Sonnet #2: bug scan", ["Sonnet #2"])).toBe(true);
    expect(todoMatchesAnyDescription("hosted pari", ["hosted pari service"])).toBe(true);
    expect(todoMatchesAnyDescription("fix", ["fixture loader"])).toBe(false);
  });

  it("collects live session job labels and lights matching pending todos", () => {
    const jobs = [
      {
        id: "j1",
        goal: "nested todo lighting",
        status: "running",
        session_id: "sess-1",
        tasks: [{ id: "t1", role: "explore", instruction: "hosted pari service", status: "running" }],
      },
    ] as Job[];
    expect(liveJobTodoLabels(jobs, "sess-1")).toEqual([
      "nested todo lighting",
      "hosted pari service",
      "explore",
    ]);
    const snapshot: SessionTodoSnapshot = {
      phases: [
        {
          name: "WP-02",
          tasks: [task("hosted pari", "pending"), task("unrelated cutover", "pending")],
        },
      ],
    };
    expect([...litTodoContents(snapshot, liveJobTodoLabels(jobs, "sess-1"))]).toEqual(["hosted pari"]);
  });

  it("keeps lit pending todos in the folded viewport", () => {
    const tasks = [
      task("old-1", "completed"),
      ...Array.from({ length: 7 }, (_, i) => task(`open-${i + 1}`, "pending")),
    ];
    const { items } = collapseTodoTasks(tasks, new Set(["open-7"]));
    expect(items.map((row) => row.content)).toContain("open-7");
  });
});
