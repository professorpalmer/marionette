import { useState } from "react";
import type { NestStatus, NestedChild, Session, SessionTodoSnapshot } from "./api";
import { useSessionStateFeed } from "./sessionStateFeed";

/**
 * Nested sessions in the UI: the rail tree, plan-step chips and note rows.
 * Design: docs/design/nested-sessions.md.
 */

export type RunnerState = "running" | "idle" | "attaching" | "missing";

export type SessionTreeNode = { row: Session; children: Session[] };

/** Children go under their parent when the parent is in the same list.
 *  A child whose parent is not listed stays at the top level. Order holds. */
export function buildSessionTree(rows: readonly Session[]): SessionTreeNode[] {
  const ids = new Set(rows.map((r) => r.id));
  const byParent = new Map<string, Session[]>();
  for (const row of rows) {
    const parent = row.nest?.parent_id;
    if (!parent || !ids.has(parent)) continue;
    const list = byParent.get(parent) ?? [];
    list.push(row);
    byParent.set(parent, list);
  }
  const nodes: SessionTreeNode[] = [];
  for (const row of rows) {
    const parent = row.nest?.parent_id;
    if (parent && ids.has(parent)) continue;
    const children = (byParent.get(row.id) ?? []).slice().sort((a, b) => (a.created ?? 0) - (b.created ?? 0));
    nodes.push({ row, children });
  }
  return nodes;
}

/** Live status: a running runner wins over the stored status. */
export function nestStatus(stored: NestStatus | undefined, runner: RunnerState | undefined): NestStatus {
  if (runner === "running" || runner === "attaching") return "working";
  return stored ?? "done";
}

export function nestStatusLabel(status: NestStatus): string {
  switch (status) {
    case "working": return "Working";
    case "done": return "Done";
    case "failed": return "Failed";
    case "stopped": return "Stopped";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

/** Dot color token for a status. */
export function nestStatusDot(status: NestStatus): string {
  switch (status) {
    case "working": return "bg-muted";
    case "done": return "bg-good";
    case "failed": return "bg-risk";
    case "stopped": return "bg-faint";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

export function nestedSessionsLabel(count: number): string {
  return count === 1 ? "1 nested session" : `${count} nested sessions`;
}

/** Child session ids that plan steps link to. */
export function linkedSessionIds(snapshot: SessionTodoSnapshot): string[] {
  const ids: string[] = [];
  for (const phase of snapshot.phases) {
    for (const task of phase.tasks) if (task.session) ids.push(task.session);
  }
  return ids;
}

/** A plan with a linked child that still works stays on screen after the turn. */
export function hasWorkingLinkedChild(snapshot: SessionTodoSnapshot, children: readonly NestedChild[]): boolean {
  const linked = new Set(linkedSessionIds(snapshot));
  return children.some((child) => linked.has(child.id) && child.status === "working");
}

const EXPANDED_KEY = "marionette.nest.expanded";

function readExpanded(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(EXPANDED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    const out: Record<string, boolean> = {};
    if (parsed && typeof parsed === "object") {
      for (const [key, value] of Object.entries(parsed)) if (typeof value === "boolean") out[key] = value;
    }
    return out;
  } catch {
    return {};
  }
}

/** The user's last open/closed choice for a parent, or undefined. */
export function storedNestExpanded(parentId: string): boolean | undefined {
  return readExpanded()[parentId];
}

export function storeNestExpanded(parentId: string, expanded: boolean): void {
  try {
    window.localStorage.setItem(EXPANDED_KEY, JSON.stringify({ ...readExpanded(), [parentId]: expanded }));
  } catch {
    /* A private window has no storage. The tree still works for this view. */
  }
}

/**
 * Children of ``sessionId`` from the shared session-state poll. The poll asks
 * about the active session, so this is live for the session on screen.
 */
export function useNestedChildren(sessionId: string): NestedChild[] {
  const [state, setState] = useState<{ sid: string; children: NestedChild[] }>({ sid: "", children: [] });
  useSessionStateFeed((reply, requestedFor) => {
    if (!sessionId || requestedFor !== sessionId) return;
    const children = reply.nest?.children ?? [];
    setState((prev) => (prev.sid === sessionId && sameChildren(prev.children, children)
      ? prev
      : { sid: sessionId, children }));
  });
  return state.sid === sessionId ? state.children : [];
}

/** The nest change counter from the shared poll. The rail reloads rows when it moves. */
export function useNestRev(): number {
  const [rev, setRev] = useState(0);
  useSessionStateFeed((reply) => {
    const next = reply.nest?.rev;
    if (typeof next === "number") setRev((prev) => (prev === next ? prev : next));
  });
  return rev;
}

function sameChildren(a: readonly NestedChild[], b: readonly NestedChild[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((child, i) => {
    const other = b[i];
    return other !== undefined && child.id === other.id && child.status === other.status
      && child.report === other.report && child.title === other.title && child.name === other.name;
  });
}
