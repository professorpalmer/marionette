import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { Ban, CheckCircle2, ChevronDown, ChevronRight, Circle, ListTree, Loader2, MinusCircle } from "lucide-react";
import { api, type Job, type SessionTodoItem, type SessionTodoSnapshot } from "../../lib/api";
import {
  collapseTodoTasks,
  litTodoContents,
  liveJobTodoLabels,
  todoHasWork,
  todoPhaseProgress,
  todoSnapshotProgress,
  toRoman,
} from "../../lib/composerTodos";
import {
  getSessionTodos,
  getSessionTodosSessionId,
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
  if (status === "in_progress" || lit) return <Loader2 size={11} className="shrink-0 animate-spin text-accent" />;
  return <Circle size={11} className="shrink-0 text-faint" />;
}

function taskTone(status: SessionTodoItem["status"], lit?: boolean): string {
  if (status === "in_progress" || lit) return "text-accent";
  if (status === "pending" || status === "abandoned") return "text-faint";
  return "text-txt";
}

export default function ComposerTodoPanel({
  jobs = [],
  sessionId,
}: {
  jobs?: readonly Job[];
  sessionId: string;
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
  const [expandedPhase, setExpandedPhase] = useState<string | null>(null);
  const lit = useMemo(
    () => litTodoContents(snapshot, liveJobTodoLabels(jobs, sessionId)),
    [jobs, sessionId, snapshot],
  );

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

  if (!todoHasWork(snapshot) || (storedSid && storedSid !== sessionId)) return null;
  const { done, total } = todoSnapshotProgress(snapshot);
  const next = snapshot.next;

  return (
    <div className={COMPOSER_FAMILY_SECTION} data-slot="composer-todo-panel">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[10.5px] leading-4 text-txt hover:bg-panel/35"
      >
        {open ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <ListTree size={11} className="text-faint" />
        <span className="font-medium tabular-nums">TODO {done}/{total}</span>
        {next && !open ? <span className="min-w-0 truncate text-faint">{next}</span> : null}
      </button>
      {open && (
        <div className="space-y-1 px-2 pb-1.5">
          {snapshot.phases.map((phase, index) => (
            <PhaseBlock
              key={`${phase.name}-${index}`}
              index={index + 1}
              phase={phase}
              expanded={expandedPhase === phase.name}
              litContents={lit}
              onToggle={() => setExpandedPhase((name) => (name === phase.name ? null : phase.name))}
            />
          ))}
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
  onToggle,
}: {
  index: number;
  phase: SessionTodoSnapshot["phases"][number];
  expanded: boolean;
  litContents: ReadonlySet<string>;
  onToggle: () => void;
}) {
  const { done, total } = todoPhaseProgress(phase);
  const { items, hidden } = expanded
    ? { items: phase.tasks, hidden: 0 }
    : collapseTodoTasks(phase.tasks, litContents);
  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 rounded-md px-0.5 py-0.5 text-left text-[10.5px] leading-4 text-txt hover:bg-panel/30"
      >
        {expanded ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <span className="font-medium tabular-nums">
          {toRoman(index)}. {phase.name} · {done}/{total}
        </span>
      </button>
      <div className="pl-4 space-y-0.5">
        {items.map((task) => {
          const lit = litContents.has(task.content);
          return (
          <div
            key={task.content}
            title={task.blocker || task.content}
            data-todo-lit={lit ? "1" : undefined}
            className={`flex items-start gap-1.5 text-[10.5px] leading-4 ${taskTone(task.status, lit)}`}
          >
            <TaskMark status={task.status} lit={lit} />
            <span className={expanded ? "whitespace-pre-wrap break-words" : "min-w-0 truncate"}>
              {task.content}
              {expanded && task.blocker ? <span className="mt-0.5 block text-faint">{task.blocker}</span> : null}
            </span>
          </div>
          );
        })}
        {hidden > 0 ? (
          <button
            type="button"
            onClick={onToggle}
            className="text-[10.5px] leading-4 text-faint hover:text-txt"
          >
            ... {hidden} more todo{hidden === 1 ? "" : "s"}
          </button>
        ) : null}
      </div>
    </div>
  );
}
