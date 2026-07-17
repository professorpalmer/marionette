/**
 * Artifact gather after sessionTranscript hydrate (session switch).
 */

import { api } from "../../lib/api";
import {
  collectDisplayArtifacts,
  mergeUniqueArtifacts,
  type SessionArtifact,
} from "./sessionHydrate";

/**
 * Collect display-card artifacts and merge with per-job artifact fetches.
 * When there are no job ids, returns synchronously (same tick as before).
 */
export function gatherSessionArtifacts(opts: {
  display: unknown;
  jobIds: string[] | undefined;
  stillCurrent: () => boolean;
}): SessionArtifact[] | Promise<SessionArtifact[]> {
  const displayArtifacts = collectDisplayArtifacts(opts.display);
  if (!opts.jobIds || opts.jobIds.length === 0) {
    return mergeUniqueArtifacts(displayArtifacts, []);
  }
  return Promise.all(
    opts.jobIds.map((jid) =>
      api.artifacts(jid)
        .then((arts) => (Array.isArray(arts) ? arts : []))
        .catch((err) => {
          console.error("Failed to fetch artifacts for job", jid, err);
          return [] as SessionArtifact[];
        }),
    ),
  ).then((allJobArts) => {
    if (!opts.stillCurrent()) return [] as SessionArtifact[];
    return mergeUniqueArtifacts(displayArtifacts, allJobArts.flat());
  });
}
