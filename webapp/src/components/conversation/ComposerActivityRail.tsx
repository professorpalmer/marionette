import { useMemo, useSyncExternalStore } from "react";
import type { Job } from "../../lib/api";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { pickTaskSourceJob } from "../../lib/composerTasks";
import ComposerStatusStack from "./ComposerStatusStack";
import ComposerTasksPanel from "./ComposerTasksPanel";
import ComposerTodoPanel, { useTodoChecklistVisible } from "./ComposerTodoPanel";
import { COMPOSER_FAMILY_SURFACE } from "./composerFamily";
import { buildComposerStatusStackRows } from "./composerStatusStackData";

export default function ComposerActivityRail({
  jobs,
  sessionId,
  active = true,
  pilotStep = null,
}: {
  jobs: readonly Job[];
  sessionId: string;
  active?: boolean;
  pilotStep?: string | null;
}) {
  const bodyJobs = jobs.filter(job => !job.metadata_only);
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
  const showTodos = useTodoChecklistVisible(bodyJobs, sessionId, active);
  const showTasks = !!pickTaskSourceJob(bodyJobs, sessionId);
  const hasOverview = showTasks || showTodos || stackRows.length > 0;

  return (
    <div
      className={hasOverview ? `mb-1 overflow-hidden ${COMPOSER_FAMILY_SURFACE}` : undefined}
      data-slot={hasOverview ? "composer-activity-rail" : undefined}
    >
      <div className={hasOverview ? "space-y-0.5 p-0.5" : undefined}>
        <ComposerTodoPanel jobs={bodyJobs} sessionId={sessionId} active={active} pilotStep={pilotStep} />
        <ComposerTasksPanel jobs={bodyJobs} sessionId={sessionId} />
        <ComposerStatusStack swarmJobs={bodyJobs} sessionId={sessionId} />
      </div>
    </div>
  );
}
