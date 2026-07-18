import { describe, expect, it } from "vitest";
import {
  autoHaltPresentation,
  autoStatusPresentation,
  commandApprovalStatusCopy,
  commandBlockedPresentation,
  formatAutoBudgetMeters,
} from "../lib/autoReceipts";
import {
  appendAutoHalt,
  appendAutoStatus,
} from "../components/conversation/streamApply";
import { isTerminalStreamKind } from "../components/conversation/chatEvents";
import type { Item } from "../components/TranscriptList";

const snap = {
  tokens_used: 4100,
  max_tokens: 50_000,
  swarms_used: 2,
  max_swarms: 20,
  elapsed_s: 45,
};

describe("autoReceipts quiet copy", () => {
  it("formats AutoBudget meters like StatusBar spend chips", () => {
    expect(formatAutoBudgetMeters(snap)).toBe("2/20 swarms · 4.1k/50k tok · 45s");
  });

  it("auto_status never implies compaction or successful execution", () => {
    const copy = autoStatusPresentation(3, snap);
    expect(copy.label).toBe("Full-auto · cycle 3");
    expect(copy.detail).toContain("2/20 swarms");
    expect(copy.label + copy.detail).not.toMatch(/compact|executed|done/i);
  });

  it("auto_halt labels stay truthful about outcome", () => {
    const met = autoHaltPresentation("objective met and verified", snap);
    expect(met.label).toBe("Full-auto finished");
    expect(met.metObjective).toBe(true);
    expect(met.detail).toContain("objective met");
    expect(met.detail).not.toMatch(/compact/i);

    const ceiling = autoHaltPresentation("swarm ceiling reached (3/3)", snap);
    expect(ceiling.label).toBe("Full-auto halted");
    expect(ceiling.metObjective).toBe(false);
    expect(ceiling.detail).not.toMatch(/executed/i);
  });

  it("command block/approval copy never claims the shell ran", () => {
    const blocked = commandBlockedPresentation({
      reason: "remote command execution",
      category: "remote-shell",
    });
    expect(blocked.label).toBe("Command not run");
    expect(blocked.detail).toContain("remote command execution");

    expect(commandApprovalStatusCopy("approved")).toMatch(/not run yet/i);
    expect(commandApprovalStatusCopy("rejected")).toMatch(/was not run/i);
  });
});

describe("auto stream append + terminal sticky contract", () => {
  it("replaces trailing auto_status and appends terminal auto_halt receipts", () => {
    let items: Item[] = [];
    items = appendAutoStatus(items, 1, { swarms_used: 0, max_swarms: 20 });
    items = appendAutoStatus(items, 2, snap);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "auto_status", cycle: 2 });

    items = appendAutoHalt(items, "swarm ceiling reached (2/20)", snap);
    expect(items).toHaveLength(2);
    expect(items[1]).toMatchObject({
      kind: "auto_halt",
      reason: "swarm ceiling reached (2/20)",
    });
    // Never a fake assistant HALT chat bubble.
    expect(items.some((it) => it.kind === "msg")).toBe(false);
  });

  it("treats auto_halt as terminal (closes sticky turnOpen) but not auto_status", () => {
    expect(isTerminalStreamKind("auto_halt")).toBe(true);
    expect(isTerminalStreamKind("auto_status")).toBe(false);
    expect(isTerminalStreamKind("command_blocked")).toBe(false);
    expect(isTerminalStreamKind("command_approval_pending")).toBe(false);
  });
});
