import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { Ban, CheckCircle2, ChevronDown, ChevronRight, Circle, ListTree, Loader2, MinusCircle, X } from "lucide-react";
import { api, type Job, type SessionTodoItem, type SessionTodoSnapshot } from "../../lib/api";
import {
  litTodoContentsFromGroups,
  liveJobTodoLabelGroups,
  sessionHasLiveTodoOwner,
  todoHasWork,
  todoPhaseProgress,
  todoSnapshotProgress,
  toRoman,
} from "../../lib/composerTodos";
import {
  getSessionTodos,
  getSessionTodosSessionId,
  getSessionTodosVersion,
  clearSessionTodos,
  publishSessionTodos,
  subscribeSessionTodos,
} from "../../lib/sessionTodos";
import { COMPOSER_FAMILY_SECTION } from "./composerFamily";

function TaskMark({
  status,
  lit,
}: {
  status: SessionTodoItem["status"];
  lit?: boolean;
}) {
  if (status === "completed") return <CheckCircle2 size={11} className="shrink-0 text-good" />;
  if (status === "abandoned") return <MinusCircle size={11} className="shrink-0 text-faint" />;
  if (status === "blocked") return <Ban size={11} className="shrink-0 text-warn" />;
  if (lit) return <Loader2 size={11} className="shrink-0 animate-spin text-accent" />;
  if (status === "in_progress") return <Circle size={11} className="shrink-0 text-accent" />;
  return <Circle size={11} className="shrink-0 text-faint" />;
}

function taskTone(status: SessionTodoItem["status"], lit?: boolean): string {
  if (status === "in_progress" || lit) return "text-accent";
  if (status === "pending" || status === "abandoned") return "text-faint";
  return "text-txt";
}

function todoPhaseKey(sessionId: string, phaseIndex: number, phaseName: string): string {
  return `${sessionId}:${phaseIndex}:${phaseName}`;
}

/**
 * The task the pilot is working on right now: the one it marked in progress,
 * else the next pending one. Between todo updates this item shows the live
 * step, so the list moves with the work without claiming anything is done.
 */
function activeTaskContent(snapshot: SessionTodoSnapshot): string | null {
  for (const phase of snapshot.phases) {
    const task = phase.tasks.find((t) => t.status === "in_progress");
    if (task) return task.content;
  }
  return snapshot.next || null;
}

/**
 * The checklist shows while an owned job is live, or while the turn is open
 * and the pilot published the list during this turn. A list from an earlier
 * turn stays hidden when the user sends a new message, until the pilot
 * updates it again.
 */
export function useTodoChecklistVisible(
  jobs: readonly Job[],
  sessionId: string,
  active: boolean,
): boolean {
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
  // Store version when the turn opened. -1: mounted mid-turn, show the list.
  const [turn, setTurn] = useState({ active, startVersion: -1 });
  if (turn.active !== active) setTurn({ active, startVersion: active ? version : -1 });
  if (!todoHasWork(snapshot) || storedSid !== sessionId) return false;
  if (sessionHasLiveTodoOwner(jobs, sessionId)) return true;
  return active && version > turn.startVersion;
}

export default function ComposerTodoPanel({
  jobs = [],
  sessionId,
  active = true,
  pilotStep = null,
}: {
  jobs?: readonly Job[];
  sessionId: string;
  active?: boolean;
  /** The pilot's running step (tool and goal) while its turn is open. */
  pilotStep?: string | null;
}) {
  const snapshot = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodos,
    getSessionTodos,
  );
  const storedSid = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodosSessionId,
    getSessionTodosSessionId,
  );
  const [open, setOpen] = useState(true);
  const [collapsedPhaseKeys, setCollapsedPhaseKeys] = useState<Set<string>>(() => new Set());
  const lit = useMemo(
    () => litTodoContentsFromGroups(snapshot, liveJobTodoLabelGroups(jobs, sessionId)),
    [jobs, sessionId, snapshot],
  );
  const visible = useTodoChecklistVisible(jobs, sessionId, active);

  useEffect(() => {
    if (!sessionId) return;
    if (storedSid === sessionId) return;
    let cancelled = false;
    api.getSessionState({ sessionId }).then((state) => {
      if (cancelled) return;
      publishSessionTodos(state.todos || { phases: [] }, sessionId);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [sessionId, storedSid]);

  if (!visible) return null;
  const { done, total } = todoSnapshotProgress(snapshot);
  const next = snapshot.next;
  const liveStep = active && pilotStep ? { task: activeTaskContent(snapshot), step: pilotStep } : null;

  const dismiss = () => {
    clearSessionTodos();
    void api.sessionTodo({ command: "/todo clear", session_id: sessionId });
  };

  return (
    <div className={COMPOSER_FAMILY_SECTION} data-slot="composer-todo-panel">
      <div className="flex items-center">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1 text-left text-ui-10.5 leading-4 text-txt hover:bg-panel/35"
        >
          {open ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
          <ListTree size={11} className="text-faint" />
          <span className="font-medium tabular-nums">TODO {done}/{total}</span>
          {next && !open ? <span className="min-w-0 truncate text-faint">{next}</span> : null}
        </button>
        <button
          type="button"
          aria-label="Dismiss TODO checklist"
          title="Dismiss TODO checklist"
          onClick={dismiss}
          className="mr-1 rounded p-1 text-faint hover:bg-panel/45 hover:text-txt"
        >
          <X size={11} />
        </button>
      </div>
      {open && (
        <div className="space-y-1 px-2 pb-1.5">
          {snapshot.phases.map((phase, index) => {
            const key = todoPhaseKey(sessionId, index, phase.name);
            return (
              <PhaseBlock
                key={key}
                index={index + 1}
                phase={phase}
                expanded={!collapsedPhaseKeys.has(key)}
                litContents={lit}
                liveStep={liveStep}
                onToggle={() => setCollapsedPhaseKeys((current) => {
                  const next = new Set(current);
                  if (next.has(key)) next.delete(key);
                  else next.add(key);
                  return next;
                })}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function PhaseBlock({
  index,
  phase,
  expanded,
  litContents,
  liveStep,
  onToggle,
}: {
  index: number;
  phase: SessionTodoSnapshot["phases"][number];
  expanded: boolean;
  litContents: ReadonlySet<string>;
  liveStep: { task: string | null; step: string } | null;
  onToggle: () => void;
}) {
  const { done, total } = todoPhaseProgress(phase);
  return (
    <div>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 rounded-md px-0.5 py-0.5 text-left text-ui-10.5 leading-4 text-txt hover:bg-panel/30"
      >
        {expanded ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <span className="font-medium tabular-nums">
          {toRoman(index)}. {phase.name} · {done}/{total}
        </span>
      </button>
      {expanded ? (
        <div className="pl-4 space-y-0.5">
          {phase.tasks.map((task) => {
            const step = liveStep && liveStep.task === task.content ? liveStep.step : null;
            const lit = litContents.has(task.content) || step != null;
            return (
              <div
                key={task.content}
                title={task.blocker || task.content}
                data-todo-lit={lit ? "1" : undefined}
                className={`flex items-start gap-1.5 text-ui-10.5 leading-4 ${taskTone(task.status, lit)}`}
              >
                <TaskMark status={task.status} lit={lit} />
                <span className="whitespace-pre-wrap break-words">
                  {task.content}
                  {task.blocker ? <span className="mt-0.5 block text-faint">{task.blocker}</span> : null}
                  {step ? <span className="block truncate font-mono text-faint">{step}</span> : null}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
