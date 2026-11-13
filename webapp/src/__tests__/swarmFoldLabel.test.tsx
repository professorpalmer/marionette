import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { swarmPendingFoldLabel, TranscriptList, type Item, type SwarmPendingItem } from "../components/TranscriptList";

afterEach(cleanup);

const pill = (status: SwarmPendingItem["status"], id = "job_aaaaaaaaaaaa"): SwarmPendingItem => ({
  kind: "swarm_pending", job_ids: [id], objective: "audit calc", status,
});

describe("swarm fold label", () => {
  it("names the state of each swarm, not a pending count", () => {
    expect(swarmPendingFoldLabel([pill("running")])).toBe("Swarm · running");
    expect(swarmPendingFoldLabel([pill("waiting")])).toBe("Swarm · waiting");
    expect(swarmPendingFoldLabel([pill("done")])).toBe("Swarm · done");
    expect(swarmPendingFoldLabel([pill("failed")])).toBe("Swarm · failed");
    expect(swarmPendingFoldLabel([pill("done"), pill("done", "job_bbbbbbbbbbbb")])).toBe("Swarm · 2 done");
    expect(swarmPendingFoldLabel([pill("done"), pill("failed", "job_bbbbbbbbbbbb")])).toBe("Swarm · 1 done, 1 failed");
    // A legacy row with only `resolved` is done.
    expect(swarmPendingFoldLabel([{ kind: "swarm_pending", job_ids: ["j"], objective: "", resolved: true }])).toBe("Swarm · done");
  });

  it("a sealed turn with a finished synchronous swarm does not say pending", () => {
    // Measured in the rig: a finished run_swarm left its terminal pill in a
    // fold of its own, and the fold read "Swarm · 1 pending".
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit it" } },
      pill("done"),
      { kind: "msg", msg: { role: "assistant", text: "Done." } },
    ];
    render(
      <TranscriptList items={items} status="done" compactingStatus={null} editingIndex={null} auto={false} plan={false}
        turnOpen={false} scrollContainerRef={{ current: null }} onEditMessage={vi.fn()} onExecuteSend={vi.fn()}
        onImageClick={vi.fn()} onSetCard={vi.fn()} onExecutePlan={vi.fn()} />,
    );
    expect(screen.queryByText(/pending/)).toBeNull();
    expect(screen.getByText("Swarm · done")).toBeInTheDocument();
  });
});
