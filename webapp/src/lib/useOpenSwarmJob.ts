import { createContext, useContext } from 'react';
import { useSharedJobMetadata, metadataJobs } from './jobMetadataContext';
import { openAgentSwarmJob } from './agentLinks';
import { swarmNavigationTarget } from './pendingSwarmOpenJob';

// Transcript ownership can lag the active metadata provider during a session switch.
export const SwarmLinkSessionContext = createContext<string | undefined>(undefined);

/** The rendered link owns its context, including when focusing the pane mounts it later. */
export function useOpenSwarmJob(sessionId?: string, repo?: string) {
  const transcriptSessionId = useContext(SwarmLinkSessionContext);
  const ownerSessionId = sessionId ?? transcriptSessionId;
  const { state } = useSharedJobMetadata();
  const context = state.view.kind === 'idle' || (ownerSessionId !== undefined && ownerSessionId !== state.view.target.session_id) || (repo !== undefined && repo !== state.view.target.repo) ? null : { ...state.view.target, contextEpoch: state.contextEpoch };
  return (jobId: string, artifactId?: string) => {
    const matches = metadataJobs(state).filter(job => job.id === jobId);
    openAgentSwarmJob(swarmNavigationTarget(jobId, context, matches.length === 1 && matches[0].read_status !== 'unavailable' ? matches[0] : undefined, artifactId));
  };
}
