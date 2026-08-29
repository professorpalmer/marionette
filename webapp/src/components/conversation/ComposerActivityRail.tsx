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
import { COMPOSER_FAMILY_HAIRLINE, COMPOSER_FAMILY_SURFACE } from "./composerFamily";
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
    () => listAgentCommandSessions(),
    [commandIndexVersion],
  );
  const stackRows = useMemo(
    () => buildComposerStatusStackRows({ swarmJobs: jobs, commandSessions }),
    [commandSessions, jobs],
  );
  const showTasks = !!pickTaskSourceJob(jobs, sessionId);
  if (!showTasks && !stackRows.length) return null;

  return (
    <div
      className={`mx-2 mb-1 overflow-hidden ${COMPOSER_FAMILY_SURFACE}`}
      data-slot="composer-activity-rail"
    >
      <div className={`divide-y ${COMPOSER_FAMILY_HAIRLINE}`}>
        <ComposerTasksPanel jobs={jobs} sessionId={sessionId} />
        <ComposerStatusStack swarmJobs={jobs} />
      </div>
    </div>
  );
}
