import { describe, expect, it } from "vitest";
import {
  collapseTodoTasks,
  litTodoContents,
  litTodoContentsFromGroups,
  liveJobTodoLabelGroups,
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

  it("lights a matching in_progress todo when a live job correlates", () => {
    const jobs = [
      {
        id: "j1",
        goal: "nested todo lighting",
        status: "running",
        session_id: "sess-1",
        tasks: [{ id: "t1", role: "explore", instruction: "hosted pari service", status: "running" }],
      },
    ] as Job[];
    const snapshot: SessionTodoSnapshot = {
      phases: [
        {
          name: "WP-02",
          tasks: [task("hosted pari", "in_progress"), task("unrelated cutover", "pending")],
        },
      ],
    };
    expect([...litTodoContents(snapshot, liveJobTodoLabels(jobs, "sess-1"))]).toEqual(["hosted pari"]);
  });

  it("does not treat a generic implement role as a match", () => {
    expect(todoMatchesAnyDescription("Implement station management server actions", ["implement"])).toBe(false);
    expect(todoMatchesAnyDescription("Implement dedicated live spectator TV", ["implement", "agentic"])).toBe(false);
    expect(todoMatchesAnyDescription("implement (agentic) WAVE 1B (Part 1 - Actions only):", ["WAVE 1B"])).toBe(true);
  });

  it("lights only the running wave when one implement job is live", () => {
    const jobs = [
      {
        id: "j-wave-1b",
        goal: "WAVE 1B (Part 1 - Actions only)",
        status: "running",
        session_id: "sess-1",
        role: "implement",
        tasks: [
          { id: "t1", role: "implement", instruction: "Implement station management server actions", status: "running" },
          { id: "t2", role: "implement", instruction: "Implement dedicated live spectator TV", status: "pending" },
          { id: "t3", role: "implement", instruction: "Implement versioned ruleset validator", status: "pending" },
        ],
      },
    ] as Job[];
    const snapshot: SessionTodoSnapshot = {
      phases: [
        {
          name: "Wave 1 — Station & Stadium Operations",
          tasks: [
            task("Ship station inventory read path", "completed"),
            task("Implement station management server actions", "in_progress"),
            task("Validate station ops", "pending"),
          ],
        },
        {
          name: "Wave 2 — Live Spectator",
          tasks: [
            task("Implement dedicated live spectator TV", "pending"),
            task("Validate spectator display", "pending"),
          ],
        },
        {
          name: "Wave 3 — Ruleset",
          tasks: [task("Implement versioned ruleset validator", "pending")],
        },
        {
          name: "Tasks",
          tasks: [task("implement (agentic) WAVE 1B (Part 1 - Actions only):", "pending")],
        },
      ],
    };
    expect(liveJobTodoLabels(jobs, "sess-1")).toEqual([
      "WAVE 1B (Part 1 - Actions only)",
      "Implement station management server actions",
    ]);
    expect([...litTodoContents(snapshot, liveJobTodoLabels(jobs, "sess-1"))]).toEqual([
      "Implement station management server actions",
    ]);
  });

  it("lights one todo per actually-running job", () => {
    const jobs = [
      {
        id: "j1",
        goal: "pari cutover",
        status: "running",
        session_id: "sess-1",
        tasks: [{ id: "t1", instruction: "hosted pari service", status: "running" }],
      },
      {
        id: "j2",
        goal: "station board",
        status: "running",
        session_id: "sess-1",
        tasks: [{ id: "t2", instruction: "station board ui", status: "running" }],
      },
    ] as Job[];
    const snapshot: SessionTodoSnapshot = {
      phases: [
        {
          name: "Work",
          tasks: [task("hosted pari", "pending"), task("station board", "pending")],
        },
      ],
    };
    expect([...litTodoContentsFromGroups(snapshot, liveJobTodoLabelGroups(jobs, "sess-1"))].sort()).toEqual([
      "hosted pari",
      "station board",
    ]);
  });

  it("lights the wave handle from a running parent before child statuses arrive", () => {
    const jobs = [
      {
        id: "j-wave-1b",
        goal: "WAVE 1B (Part 1 - Actions only)",
        status: "running",
        session_id: "sess-1",
        role: "implement",
        tasks: [
          { id: "t1", role: "implement", instruction: "Implement station management server actions", status: "pending" },
          { id: "t2", role: "implement", instruction: "Implement dedicated live spectator TV", status: "pending" },
        ],
      },
    ] as Job[];
    const snapshot: SessionTodoSnapshot = {
      phases: [
        {
          name: "Wave 1",
          tasks: [task("Implement station management server actions", "in_progress")],
        },
        {
          name: "Wave 2",
          tasks: [task("Implement dedicated live spectator TV", "pending")],
        },
        {
          name: "Tasks",
          tasks: [task("implement (agentic) WAVE 1B (Part 1 - Actions only):", "pending")],
        },
      ],
    };
    expect(liveJobTodoLabels(jobs, "sess-1")).toEqual(["WAVE 1B (Part 1 - Actions only)"]);
    expect([...litTodoContents(snapshot, liveJobTodoLabels(jobs, "sess-1"))]).toEqual([
      "implement (agentic) WAVE 1B (Part 1 - Actions only):",
    ]);
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
