import { afterEach, describe, expect, it } from "vitest";
import { appendMemoryProposal } from "../components/conversation/swarmPoll";
import {
  forgetResolvedMemoryProposal,
  getActiveMemoryProposalSession,
  isResolvedMemoryProposal,
  rememberResolvedMemoryProposal,
  setActiveMemoryProposalSession,
} from "../lib/memoryProposalResolution";

describe("memoryProposalResolution", () => {
  afterEach(() => {
    sessionStorage.clear();
    setActiveMemoryProposalSession("");
  });

  it("remembers Save across session switch so replay cannot resurrect the card", () => {
    rememberResolvedMemoryProposal("sess-a", "memprop_1");
    setActiveMemoryProposalSession("sess-b");
    expect(isResolvedMemoryProposal("sess-a", "memprop_1")).toBe(true);
    setActiveMemoryProposalSession("sess-a");
    expect(isResolvedMemoryProposal(getActiveMemoryProposalSession(), "memprop_1")).toBe(true);
    forgetResolvedMemoryProposal("sess-a", "memprop_1");
    expect(isResolvedMemoryProposal("sess-a", "memprop_1")).toBe(false);
  });

  it("appendMemoryProposal ignores a saved id after session switch replay", () => {
    setActiveMemoryProposalSession("sess-a");
    rememberResolvedMemoryProposal("sess-a", "memprop_1");
    expect(appendMemoryProposal([], { id: "memprop_1", text: " demo kit", category: "fact" })).toEqual([]);
  });
});

describe("memoryProposalResolution", () => {
  afterEach(() => {
    sessionStorage.clear();
    setActiveMemoryProposalSession("");
  });

  it("remembers Save across session switch so replay cannot resurrect the card", () => {
    rememberResolvedMemoryProposal("sess-a", "memprop_1");
    setActiveMemoryProposalSession("sess-b");
    expect(isResolvedMemoryProposal("sess-a", "memprop_1")).toBe(true);
    setActiveMemoryProposalSession("sess-a");
    expect(isResolvedMemoryProposal(getActiveMemoryProposalSession(), "memprop_1")).toBe(true);
    forgetResolvedMemoryProposal("sess-a", "memprop_1");
    expect(isResolvedMemoryProposal("sess-a", "memprop_1")).toBe(false);
  });
});
