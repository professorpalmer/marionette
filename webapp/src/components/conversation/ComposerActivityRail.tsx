import MetadataActivity from './MetadataActivity';
import { useMemo, useSyncExternalStore } from "react";
import type { Job } from "../../lib/api";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { pickTaskSourceJob } from "../../lib/composerTasks";
import { todoHasWork } from "../../lib/composerTodos";
import { getSessionTodos, getSessionTodosSessionId, subscribeSessionTodos } from "../../lib/sessionTodos";
import ComposerStatusStack from "./ComposerStatusStack";
import ComposerTasksPanel from "./ComposerTasksPanel";
import ComposerTodoPanel from "./ComposerTodoPanel";
import { COMPOSER_FAMILY_SURFACE } from "./composerFamily";
import { buildComposerStatusStackRows } from "./composerStatusStackData";

export default function ComposerActivityRail({
  jobs,
  sessionId,
}: {
  jobs: readonly Job[];
  sessionId: string;
}) {
  const bodyJobs = jobs.filter(job => !job.metadata_only);
  const observedJobs = jobs.filter(job => job.metadata_only && job.session_id === sessionId);
  const commandIndexVersion = useSyncExternalStore(
    subscribeAgentCommandIndex,
    getAgentCommandIndexVersion,
    getAgentCommandIndexVersion,
  );
  const commandSessions = useMemo(
    () => listAgentCommandSessions(sessionId),
    [commandIndexVersion, sessionId],
  );
  const stackRows = useMemo(
    () => buildComposerStatusStackRows({ swarmJobs: bodyJobs, commandSessions, sessionId }),
    [commandSessions, bodyJobs, sessionId],
  );
  const todos = useSyncExternalStore(subscribeSessionTodos, getSessionTodos, getSessionTodos);
  const todoSessionId = useSyncExternalStore(
    subscribeSessionTodos,
    getSessionTodosSessionId,
    getSessionTodosSessionId,
  );
  const showTodos = todoHasWork(todos) && todoSessionId === sessionId;
  const showTasks = !!pickTaskSourceJob(bodyJobs, sessionId);
  const hasOverview = showTasks || showTodos || stackRows.length > 0 || observedJobs.length > 0;

  return (
    <div
      className={hasOverview ? `mb-1 overflow-hidden ${COMPOSER_FAMILY_SURFACE}` : undefined}
      data-slot={hasOverview ? "composer-activity-rail" : undefined}
    >
      <div className={hasOverview ? "space-y-0.5 p-0.5" : undefined}>
        <ComposerTodoPanel jobs={bodyJobs} sessionId={sessionId} />
        <ComposerTasksPanel jobs={bodyJobs} sessionId={sessionId} />
        <MetadataActivity key={sessionId} jobs={observedJobs} />
        <ComposerStatusStack swarmJobs={bodyJobs} sessionId={sessionId} />
      </div>
    </div>
  );
}
