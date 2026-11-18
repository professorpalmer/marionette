import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { Ban, CircleCheck, CircleDashed, CircleEllipsis, CircleMinus, Loader2, X } from "lucide-react";
import { api, type Job, type NestedChild, type SessionTodoItem, type SessionTodoSnapshot } from "../../lib/api";
import { hasWorkingLinkedChild, nestStatusDot, nestStatusLabel, useNestedChildren } from "../../lib/nestedSessions";
import { requestSessionSwitch } from "../../lib/sessionNav";
import {
  collapseTodoTasks,
  litTodoContentsFromGroups,
  liveJobTodoLabelGroups,
  sessionHasLiveTodoOwner,
  todoHasWork,
  todoPhaseProgress,
  todoSnapshotProgress,
} from "../../lib/composerTodos";
import {
  getSessionTodos,
  getSessionTodosSessionId,
  getSessionTodosVersion,
  clearSessionTodos,
  publishSessionTodos,
  subscribeSessionTodos,
} from "../../lib/sessionTodos";

/**
 * How the plan reads:
 * - live: the pilot wrote or updated it in the open turn, or a job or a
 *   nested session of this session still works on it.
 * - stale: a turn is open, the plan has open steps, and the pilot did not
 *   touch it in this turn yet.
 * - settled: the turn that wrote the plan is closed and steps are still open.
 */
export type PlanCardMode = "live" | "stale" | "settled";

/** A plan longer than this shows its closed lead and next open steps only. */
export const PLAN_FULL_LIST_MAX = 8;

const OPEN = new Set<SessionTodoItem["status"]>(["pending", "in_progress", "blocked"]);

function planHasOpenStep(snapshot: SessionTodoSnapshot): boolean {
  return snapshot.phases.some((phase) => phase.tasks.some((task) => OPEN.has(task.status)));
}

/** Load the stored plan of the session on screen into the shared plan store. */
export function useSessionPlanSync(sessionId: string): void {
  const storedSid = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodosSessionId,
    getSessionTodosSessionId,
  );
  useEffect(() => {
    if (!sessionId || storedSid === sessionId) return;
    let cancelled = false;
    api.getSessionState({ sessionId }).then((state) => {
      if (cancelled) return;
      publishSessionTodos(state.todos || { phases: [] }, sessionId);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [sessionId, storedSid]);
}

export function usePlanCardMode(
  jobs: readonly Job[],
  sessionId: string,
  active: boolean,
): PlanCardMode | null {
  const snapshot = useSyncExternalStore(subscribeSessionTodos, getSessionTodos, getSessionTodos);
  const storedSid = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodosSessionId,
    getSessionTodosSessionId,
  );
  const version = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodosVersion,
    getSessionTodosVersion,
  );
  const children = useNestedChildren(sessionId);
  // startVersion: store version when the turn opened (-1: mounted in an open
  // turn, so the plan counts as current). published: the turn that just ended
  // wrote the plan.
  const [turn, setTurn] = useState({ active, startVersion: -1, published: false });
  if (turn.active !== active) {
    setTurn(active
      ? { active, startVersion: version, published: false }
      : { active, startVersion: -1, published: version > turn.startVersion });
  }
  if (!todoHasWork(snapshot) || storedSid !== sessionId) return null;
  if (sessionHasLiveTodoOwner(jobs, sessionId) || hasWorkingLinkedChild(snapshot, children)) return "live";
  const open = planHasOpenStep(snapshot);
  if (active) {
    if (version > turn.startVersion) return "live";
    return open ? "stale" : null;
  }
  return open && turn.published ? "settled" : null;
}

function StepMark({ status, lit }: { status: SessionTodoItem["status"]; lit: boolean }) {
  const cls = "mt-[3px] shrink-0";
  if (status === "completed") return <CircleCheck size={14} className={`${cls} text-faint`} />;
  if (status === "abandoned") return <CircleMinus size={14} className={`${cls} text-faint`} />;
  if (status === "blocked") return <Ban size={14} className={`${cls} text-warn`} />;
  if (lit) return <Loader2 size={14} className={`${cls} animate-spin text-accent`} />;
  if (status === "in_progress") return <CircleEllipsis size={14} className={`${cls} text-txt`} />;
  return <CircleDashed size={14} className={`${cls} text-faint`} />;
}

function stepTone(status: SessionTodoItem["status"], lit: boolean): string {
  if (status === "in_progress" || lit) return "text-txt";
  if (status === "blocked") return "text-muted";
  if (status === "pending") return "text-muted";
  return "text-faint";
}

const STATUS_WORD: Record<SessionTodoItem["status"], string> = {
  pending: "Not started",
  in_progress: "In progress",
  completed: "Done",
  abandoned: "Dropped",
  blocked: "Blocked",
};

/**
 * The pilot's plan for the work in hand, under the latest message: one row
 * for each step, with the step in progress bright and the rest dim.
 */
export default function PlanCard({
  jobs = [],
  sessionId,
  active = true,
}: {
  jobs?: readonly Job[];
  sessionId: string;
  /** The pilot's turn is open. */
  active?: boolean;
}) {
  const snapshot = useSyncExternalStore(subscribeSessionTodos, getSessionTodos, getSessionTodos);
  const [showAll, setShowAll] = useState(false);
  const lit = useMemo(
    () => litTodoContentsFromGroups(snapshot, liveJobTodoLabelGroups(jobs, sessionId)),
    [jobs, sessionId, snapshot],
  );
  const mode = usePlanCardMode(jobs, sessionId, active);
  const children = useNestedChildren(sessionId);
  if (!mode) return null;

  const { done, total } = todoSnapshotProgress(snapshot);
  const header = mode === "stale"
    ? "Plan from before your last message"
    : mode === "settled"
      ? `Plan · ${done} of ${total} steps done`
      : null;
  const named = snapshot.phases.length > 1;
  const folded = !showAll && total > PLAN_FULL_LIST_MAX;
  let hidden = 0;
  const phases = snapshot.phases.map((phase) => {
    if (!folded) return { phase, tasks: phase.tasks };
    const cut = collapseTodoTasks(phase.tasks, lit);
    hidden += cut.hidden;
    return { phase, tasks: cut.items };
  });

  const dismiss = () => {
    clearSessionTodos();
    void api.sessionTodo({ command: "/todo clear", session_id: sessionId });
  };

  return (
    <section
      aria-label="Plan"
      data-slot="plan-card"
      data-plan-mode={mode}
      className="group/plan relative mt-2 rounded-xl border border-edge/60 px-3.5 py-2.5 font-sans text-ui-13 leading-snug select-none"
    >
      <button
        type="button"
        aria-label="Dismiss plan"
        title="Dismiss plan"
        onClick={dismiss}
        className="absolute right-1.5 top-1.5 rounded p-1 text-faint opacity-0 hover:bg-panel2/60 hover:text-txt focus-visible:opacity-100 group-hover/plan:opacity-100"
      >
        <X size={12} />
      </button>
      {header ? <div className="mb-1.5 pr-6 text-ui-11 text-faint" data-testid="plan-card-header">{header}</div> : null}
      <div className="flex flex-col gap-2">
        {phases.map(({ phase, tasks }, index) => {
          if (!tasks.length) return null;
          const progress = todoPhaseProgress(phase);
          return (
            <div key={`${index}:${phase.name}`}>
              {named ? (
                <div className="mb-1 text-ui-11 text-faint tabular-nums">
                  {phase.name} · {progress.done}/{progress.total}
                </div>
              ) : null}
              <ul className="flex flex-col gap-1.5">
                {tasks.map((task) => {
                  const isLit = lit.has(task.content);
                  return (
                    <li
                      key={task.content}
                      title={`${STATUS_WORD[task.status]}${task.blocker ? `: ${task.blocker}` : ""}`}
                      aria-current={task.status === "in_progress" ? "step" : undefined}
                      data-plan-status={task.status}
                      data-todo-lit={isLit ? "1" : undefined}
                      className={`flex items-start gap-2 ${stepTone(task.status, isLit)}`}
                    >
                      <StepMark status={task.status} lit={isLit} />
                      <span className="min-w-0 whitespace-pre-wrap break-words">
                        {task.content}
                        <span className="sr-only"> ({STATUS_WORD[task.status]})</span>
                        {task.blocker ? <span className="block text-ui-11 text-faint">{task.blocker}</span> : null}
                        {task.session ? (
                          <SessionChip
                            id={task.session}
                            name={task.session_name || task.session}
                            child={children.find((c) => c.id === task.session)}
                          />
                        ) : null}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
      {total > PLAN_FULL_LIST_MAX ? (
        <button
          type="button"
          aria-expanded={!folded}
          onClick={() => setShowAll((value) => !value)}
          className="mt-1.5 text-ui-11 text-faint hover:text-txt"
        >
          {folded ? `Show all ${total} steps (${hidden} more)` : "Show fewer steps"}
        </button>
      ) : null}
    </section>
  );
}

/** A plan step's nested session: name and live status. A click opens it. */
function SessionChip({ id, name, child }: { id: string; name: string; child: NestedChild | undefined }) {
  const status = child?.status;
  return (
    <button
      type="button"
      onClick={() => requestSessionSwitch(id)}
      title={child?.report ? `${child.title}\n${child.report}` : `Open ${name}`}
      data-session-chip={id}
      className="mt-0.5 flex max-w-full items-center gap-1.5 rounded-md bg-panel/45 px-1.5 py-0.5 text-ui-11 leading-4 text-txt hover:bg-panel/70"
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${status ? nestStatusDot(status) : "bg-faint"}`} />
      <span className="min-w-0 truncate">{name}</span>
      {status ? <span className="shrink-0 text-faint">· {nestStatusLabel(status)}</span> : null}
    </button>
  );
}
