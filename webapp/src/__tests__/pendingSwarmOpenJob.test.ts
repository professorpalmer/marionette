/**
 * Pending swarm deep-link queue: survives late SwarmPane mount.
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  clearPendingSwarmOpenJob,
  peekPendingSwarmOpenJob,
  queuePendingSwarmOpenJob,
  takePendingSwarmOpenJob,
} from "../lib/pendingSwarmOpenJob";
import { openAgentSwarmJob } from "../lib/agentLinks";

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

  it("ignores blank ids", () => {
    queuePendingSwarmOpenJob("   ");
    expect(peekPendingSwarmOpenJob()).toBeNull();
  });
});
