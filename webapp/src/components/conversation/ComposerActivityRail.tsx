import { useMemo, useSyncExternalStore } from "react";
import type { Job } from "../../lib/api";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { pickTaskSourceJob } from "../../lib/composerTasks";
import { todoHasWork } from "../../lib/composerTodos";
import { getSessionTodos, subscribeSessionTodos } from "../../lib/sessionTodos";
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
    () => buildComposerStatusStackRows({ swarmJobs: jobs, commandSessions, sessionId }),
    [commandSessions, jobs, sessionId],
  );
  const todos = useSyncExternalStore(subscribeSessionTodos, getSessionTodos, getSessionTodos);
  const showTodos = todoHasWork(todos);
  const showTasks = !!pickTaskSourceJob(jobs, sessionId);
  if (!showTasks && !showTodos && !stackRows.length) return null;

  return (
    <div
      className={`mb-1 overflow-hidden ${COMPOSER_FAMILY_SURFACE}`}
      data-slot="composer-activity-rail"
    >
      <div className="space-y-0.5 p-0.5">
        <ComposerTodoPanel jobs={jobs} sessionId={sessionId} />
        <ComposerTasksPanel jobs={jobs} sessionId={sessionId} />
        <ComposerStatusStack swarmJobs={jobs} sessionId={sessionId} />
      </div>
    </div>
  );
}
