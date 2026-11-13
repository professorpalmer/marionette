/**
 * Pending swarm deep-link queue: survives late SwarmPane mount.
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  clearPendingSwarmOpenJob,
  peekPendingSwarmOpenJob,
  pinnedJobIds,
  queuePendingSwarmOpenJob,
  swarmNavigationTarget,
  takePendingSwarmOpenArtifact,
  takePendingSwarmOpenJob,
} from "../lib/pendingSwarmOpenJob";
import { openAgentSwarmJob } from "../lib/agentLinks";
import { filterJobsByScope } from "../lib/jobScope";

afterEach(() => {
  clearPendingSwarmOpenJob();
});

describe("pendingSwarmOpenJob queue", () => {
  it("survives until a late consumer takes it (SwarmPane mount race)", () => {
    // Simulate openAgentSwarmJob while SwarmPane is unmounted: queue + fire
    // events that nobody hears yet.
    openAgentSwarmJob("job_abcdef012345");
    expect(peekPendingSwarmOpenJob()).toBe("job_abcdef012345");

    // Late mount drains the queue — expand/scroll target is preserved.
    expect(takePendingSwarmOpenJob()).toBe("job_abcdef012345");
    expect(peekPendingSwarmOpenJob()).toBeNull();
    expect(takePendingSwarmOpenJob()).toBeNull();
  });

  it("queues overwrite so the latest deep-link wins", () => {
    queuePendingSwarmOpenJob("local-bf1b30f4");
    queuePendingSwarmOpenJob("job_abcdef012345");
    expect(takePendingSwarmOpenJob()).toBe("job_abcdef012345");
  });

  it("carries an exact artifact target through a late mount", () => {
    openAgentSwarmJob("job_abcdef012345", "artifact-target");
    expect(takePendingSwarmOpenJob()).toBe("job_abcdef012345");
    expect(takePendingSwarmOpenArtifact()).toBe("artifact-target");
  });

  it("ignores blank ids", () => {
    queuePendingSwarmOpenJob("   ");
    expect(peekPendingSwarmOpenJob()).toBeNull();
  });
});

describe("pinnedJobIds", () => {
  const job = { id: "job_a", session_id: "s1" };
  const pin = (session_id: string | null) => swarmNavigationTarget("job_a",
    session_id === null ? null : { repo: "/r", session_id, contextEpoch: 1 });

  it("widens the Session view only in the session where the pin was made", () => {
    expect(pinnedJobIds(pin("s2"), "s2")).toEqual(["job_a"]);
    expect(filterJobsByScope([job], "session", "s2", { includeJobIds: pinnedJobIds(pin("s2"), "s2") })).toEqual([job]);
  });

  it("does not carry a pin from the previous session into a new one", () => {
    // The review saw this: an unresolved pin from session s1 listed s1's job in a new session.
    expect(filterJobsByScope([job], "session", "s2", { includeJobIds: ["job_a"] })).toEqual([job]);
    expect(pinnedJobIds(pin("s1"), "s2")).toBeUndefined();
    expect(filterJobsByScope([job], "session", "s2", { includeJobIds: pinnedJobIds(pin("s1"), "s2") })).toEqual([]);
  });

  it("does not widen the view for a pin with no known session", () => {
    expect(pinnedJobIds(pin(null), "s2")).toBeUndefined();
    expect(pinnedJobIds(null, "s2")).toBeUndefined();
    expect(pinnedJobIds(pin("s2"), " ")).toBeUndefined();
  });
});
