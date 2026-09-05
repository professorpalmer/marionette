import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { Ban, CheckCircle2, ChevronDown, ChevronRight, Circle, ListTree, Loader2, MinusCircle } from "lucide-react";
import { api, type Job, type SessionTodoItem, type SessionTodoSnapshot } from "../../lib/api";
import {
  litTodoContentsFromGroups,
  liveJobTodoLabelGroups,
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
  const [collapsedPhaseKeys, setCollapsedPhaseKeys] = useState<Set<string>>(() => new Set());
  const lit = useMemo(
    () => litTodoContentsFromGroups(snapshot, liveJobTodoLabelGroups(jobs, sessionId)),
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

  if (!todoHasWork(snapshot) || storedSid !== sessionId) return null;
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
          {snapshot.phases.map((phase, index) => {
            const key = todoPhaseKey(sessionId, index, phase.name);
            return (
              <PhaseBlock
                key={key}
                index={index + 1}
                phase={phase}
                expanded={!collapsedPhaseKeys.has(key)}
                litContents={lit}
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
  onToggle,
}: {
  index: number;
  phase: SessionTodoSnapshot["phases"][number];
  expanded: boolean;
  litContents: ReadonlySet<string>;
  onToggle: () => void;
}) {
  const { done, total } = todoPhaseProgress(phase);
  return (
    <div>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 rounded-md px-0.5 py-0.5 text-left text-[10.5px] leading-4 text-txt hover:bg-panel/30"
      >
        {expanded ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <span className="font-medium tabular-nums">
          {toRoman(index)}. {phase.name} · {done}/{total}
        </span>
      </button>
      {expanded ? (
        <div className="pl-4 space-y-0.5">
          {phase.tasks.map((task) => {
            const lit = litContents.has(task.content);
            return (
              <div
                key={task.content}
                title={task.blocker || task.content}
                data-todo-lit={lit ? "1" : undefined}
                className={`flex items-start gap-1.5 text-[10.5px] leading-4 ${taskTone(task.status, lit)}`}
              >
                <TaskMark status={task.status} lit={lit} />
                <span className="whitespace-pre-wrap break-words">
                  {task.content}
                  {task.blocker ? <span className="mt-0.5 block text-faint">{task.blocker}</span> : null}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
