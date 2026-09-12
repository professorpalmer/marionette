import { describe, expect, it } from "vitest";
import { metadataActivity } from "../lib/jobMetadataContext";
import type { JobMetadataState } from "../lib/useJobMetadata";
import { JobMetadataStore } from "../lib/useJobMetadata";
import type { LocalObservation, LocalSummary } from "../lib/localJobMetadata";
import { summary } from "./jobMetadata.fixtures";

function localObs(id: string, kind: LocalSummary["kind"], lifecycle = "running"): LocalObservation {
  return {
    freshness: "observed",
    observedAt: 1,
    row: {
      local_ref: { job_id: id, incarnation: "inc" },
      revision: 1,
      deleted: false,
      session_id: "s",
      lifecycle,
      kind,
      parent_ref: null,
      task_count: 0,
      action_count: 0,
      artifact_count: 0,
      child_count: null,
      created_at: 1,
      updated_at: 1,
      receipts: { terminal: false, launch: false, recovery: false, child: false },
      economics: { kind: "unavailable" },
    },
  };
}

function withLocals(observations: LocalObservation[]): JobMetadataState {
  const snapshot = new JobMetadataStore().getSnapshot();
  return {
    ...snapshot,
    local: { ...snapshot.local, observations },
  };
}

describe("metadataActivity live dot", () => {
  it("does not pulse for a running run_command / local-cmd process", () => {
    const state = withLocals([
      localObs("local-cmd-1", "run_command"),
      localObs("local-cmdbatch-2", "run_command_batch"),
      localObs("leaf-provider", "provider"),
    ]);
    expect(metadataActivity(state).count).toBe(0);
  });

  it("pulses for a running local swarm hire", () => {
    const state = withLocals([localObs("local-swarm-dispatch-1", "provider")]);
    expect(metadataActivity(state).count).toBe(1);
  });

  it("pulses for an observed running PM job_* row even on an idle view", () => {
    const snapshot = new JobMetadataStore().getSnapshot();
    const state: JobMetadataState = {
      ...snapshot,
      observations: [{ row: summary(), freshness: "observed" }],
    };
    expect(metadataActivity(state).count).toBe(1);
  });

  it("does not pulse a queued local command that is merely registered", () => {
    const state = withLocals([localObs("local-cmd-queued", "run_command", "registered")]);
    expect(metadataActivity(state).count).toBe(0);
  });
});
