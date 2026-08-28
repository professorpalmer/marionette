import { describe, expect, it } from "vitest";
import {
  isSwarmPendingTerminalStatus,
  swarmPendingStatusRank,
} from "../components/conversation/swarmPendingIdentity";

describe("swarmPendingStatusRank", () => {
  it("ranks failed above partial above done", () => {
    expect(swarmPendingStatusRank("failed")).toBeGreaterThan(swarmPendingStatusRank("partial"));
    expect(swarmPendingStatusRank("partial")).toBeGreaterThan(swarmPendingStatusRank("done"));
    expect(swarmPendingStatusRank("done")).toBeGreaterThan(swarmPendingStatusRank("ended"));
    expect(swarmPendingStatusRank("ended")).toBeGreaterThan(swarmPendingStatusRank("running"));
  });

  it("treats partial as terminal", () => {
    expect(isSwarmPendingTerminalStatus("partial")).toBe(true);
    expect(isSwarmPendingTerminalStatus("failed")).toBe(true);
    expect(isSwarmPendingTerminalStatus("done")).toBe(true);
    expect(isSwarmPendingTerminalStatus("ended")).toBe(true);
    expect(isSwarmPendingTerminalStatus("running")).toBe(false);
  });
});
