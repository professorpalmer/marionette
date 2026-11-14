import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NestedChild, Session, SessionTodoSnapshot } from "../lib/api";
import {
  buildSessionTree,
  hasWorkingLinkedChild,
  nestStatus,
  nestedSessionsLabel,
  storeNestExpanded,
  storedNestExpanded,
} from "../lib/nestedSessions";
import NestedSessionRows from "../components/NestedSessionRows";
import { parseSessionNote, plainReport, SessionNoteRow } from "../components/conversation/SessionNoteRow";
import { appendSessionNote } from "../components/conversation/streamApply";
import { SWITCH_SESSION_EVENT } from "../lib/sessionNav";

afterEach(() => localStorage.clear());

const row = (id: string, created: number, parent?: string, extra: Partial<Session> = {}): Session => ({
  id,
  title: id,
  created,
  ...(parent ? { nest: { parent_id: parent, name: `${id}-name` } } : {}),
  ...extra,
});

const child = (id: string, status: NestedChild["status"]): NestedChild => ({
  id, title: id, name: id, status, report: "", todo: "", branch: "", worktree: "", updated: 0,
});

describe("buildSessionTree", () => {
  it("puts children under their parent in created order and keeps top-level order", () => {
    const tree = buildSessionTree([
      row("p2", 5),
      row("c2", 9, "p1"),
      row("p1", 1),
      row("c1", 3, "p1"),
    ]);
    expect(tree.map((n) => n.row.id)).toEqual(["p2", "p1"]);
    expect(tree[1]?.children.map((c) => c.id)).toEqual(["c1", "c2"]);
    expect(tree[0]?.children).toEqual([]);
  });

  it("keeps a child at the top level when its parent is not in the list", () => {
    const tree = buildSessionTree([row("orphan", 1, "gone")]);
    expect(tree.map((n) => n.row.id)).toEqual(["orphan"]);
  });
});

describe("status and labels", () => {
  it("a running runner is Working, else the stored status", () => {
    expect(nestStatus("done", "running")).toBe("working");
    expect(nestStatus("failed", "idle")).toBe("failed");
    expect(nestStatus(undefined, undefined)).toBe("done");
  });

  it("counts nested sessions", () => {
    expect(nestedSessionsLabel(1)).toBe("1 nested session");
    expect(nestedSessionsLabel(2)).toBe("2 nested sessions");
  });

  it("keeps the plan visible while a linked child works", () => {
    const snapshot: SessionTodoSnapshot = {
      phases: [{ name: "Plan", tasks: [{ content: "a", status: "in_progress", session: "c1", session_name: "a" }] }],
    };
    expect(hasWorkingLinkedChild(snapshot, [child("c1", "working")])).toBe(true);
    expect(hasWorkingLinkedChild(snapshot, [child("c1", "done")])).toBe(false);
    expect(hasWorkingLinkedChild(snapshot, [child("c2", "working")])).toBe(false);
  });

  it("remembers the open state per parent", () => {
    expect(storedNestExpanded("p1")).toBeUndefined();
    storeNestExpanded("p1", false);
    expect(storedNestExpanded("p1")).toBe(false);
  });
});

describe("NestedSessionRows", () => {
  const kids = [row("c1", 1, "p1", { title: "Wrong vehicle return place" }), row("c2", 2, "p1", { title: "Robot change" })];

  it("collapsed shows one count row that opens the tree", () => {
    const onToggle = vi.fn();
    render(
      <NestedSessionRows sessions={kids} expanded={false} onToggle={onToggle} runners={{}}
        switchingSessionId={null} onOpen={vi.fn()} onPrefetch={vi.fn()} />,
    );
    fireEvent.click(screen.getByText("2 nested sessions"));
    expect(onToggle).toHaveBeenCalled();
  });

  it("open shows each child, a dot only while working, and opens on click", () => {
    const onOpen = vi.fn();
    render(
      <NestedSessionRows sessions={kids} expanded onToggle={vi.fn()} runners={{ c2: "running" }}
        switchingSessionId={null} onOpen={onOpen} onPrefetch={vi.fn()} />,
    );
    expect(screen.getByText("Wrong vehicle return place")).toBeTruthy();
    expect(screen.getAllByLabelText("Working")).toHaveLength(1);
    fireEvent.click(screen.getByText("Robot change"));
    expect(onOpen).toHaveBeenCalledWith("c2");
  });
});

describe("session notes", () => {
  it("parses a display row and drops a row with no child", () => {
    expect(parseSessionNote({ type: "session_note", child_id: "c1", name: "full-bag", status: "done", report: "ok", ts: 5 }))
      .toEqual({ kind: "session_note", child_id: "c1", name: "full-bag", status: "done", report: "ok", driver: "parent", ts: 5 });
    expect(parseSessionNote({ type: "session_note" })).toBeNull();
    expect(parseSessionNote(null)).toBeNull();
  });

  it("shows the report without markdown marks", () => {
    expect(plainReport("**Commit:** `abc` and __x__")).toBe("Commit: abc and x");
  });

  it("appends one row per report", () => {
    const note = parseSessionNote({ child_id: "c1", ts: 7 });
    if (!note) throw new Error("note");
    const once = appendSessionNote([], note);
    expect(appendSessionNote(once, note)).toBe(once);
  });

  it("the row opens the child session", () => {
    const note = parseSessionNote({ child_id: "c9", name: "robot-change", status: "working", ts: 1 });
    if (!note) throw new Error("note");
    const seen: string[] = [];
    const listener = (e: Event) => {
      const detail: unknown = (e as CustomEvent<unknown>).detail;
      if (detail && typeof detail === "object" && "sessionId" in detail && typeof detail.sessionId === "string") {
        seen.push(detail.sessionId);
      }
    };
    window.addEventListener(SWITCH_SESSION_EVENT, listener);
    render(<SessionNoteRow item={note} />);
    expect(screen.getByText("· Working")).toBeTruthy();
    fireEvent.click(screen.getByText("robot-change"));
    window.removeEventListener(SWITCH_SESSION_EVENT, listener);
    expect(seen).toEqual(["c9"]);
  });
});
