import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  type Item,
  type SwarmPendingItem,
} from "../components/TranscriptList";
import { appendSwarmPending } from "../components/conversation/streamApply";
import {
  classifySwarmPollEvent,
  pendingJobIdsAfterSwarmPending,
} from "../components/conversation/swarmPoll";
import {
  mergeTranscriptItems,
  transcriptResponseToItems,
} from "../components/conversation/transcriptItems";

const RUN = "flow_aaaaaaaaaaaa";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function pill(items: Item[]): SwarmPendingItem {
  const found = items.filter((it): it is SwarmPendingItem => it.kind === "swarm_pending");
  expect(found).toHaveLength(1);
  return found[0];
}

describe("flow waiting pill", () => {
  it("explicit status flips a live pill between running and waiting", () => {
    let items = appendSwarmPending([], [RUN], "Flow g: ship", "running");
    expect(pill(items).status).toBe("running");
    items = appendSwarmPending(items, [RUN], "Flow g: ship", "waiting");
    expect(pill(items)).toMatchObject({ status: "waiting", resolved: false });
    // A replayed frame without a status keeps the waiting pill waiting.
    items = appendSwarmPending(items, [RUN], "Flow g: ship");
    expect(pill(items).status).toBe("waiting");
    items = appendSwarmPending(items, [RUN], "Flow g: ship", "running");
    expect(pill(items).status).toBe("running");
  });

  it("an explicit status never reopens a terminal pill", () => {
    const done: Item[] = [{ kind: "swarm_pending", job_ids: [RUN], objective: "o", status: "done", resolved: true }];
    expect(pill(appendSwarmPending(done, [RUN], "o", "running")).status).toBe("done");
    expect(pill(appendSwarmPending(done, [RUN], "o", "waiting")).status).toBe("done");
  });

  it("a new pill can start waiting", () => {
    expect(pill(appendSwarmPending([], [RUN], "o", "waiting"))).toMatchObject({ status: "waiting", resolved: false });
  });

  it("waiting releases Still working pending ids; running adds them once", () => {
    expect(pendingJobIdsAfterSwarmPending([RUN, "job_x"], [RUN], "waiting")).toEqual(["job_x"]);
    expect(pendingJobIdsAfterSwarmPending(["job_x"], [RUN], "running")).toEqual(["job_x", RUN]);
    expect(pendingJobIdsAfterSwarmPending([RUN], [RUN], undefined)).toEqual([RUN]);
  });

  it("the swarm-results poll classifies a waiting swarm_pending", () => {
    expect(classifySwarmPollEvent({
      kind: "swarm_pending",
      data: { job_ids: [RUN], objective: "o", status: "waiting" },
    })).toEqual({ kind: "swarm_pending", jobIds: [RUN], objective: "o", status: "waiting" });
    expect(classifySwarmPollEvent({
      kind: "swarm_pending",
      data: { job_ids: [RUN], objective: "o", status: "bogus" },
    })).toEqual({ kind: "swarm_pending", jobIds: [RUN], objective: "o", status: undefined });
  });

  it("hydrate keeps a persisted waiting pill waiting", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "ship it" },
        { type: "swarm_pending", job_ids: [RUN], objective: "Flow g: ship", status: "waiting", session_id: "s" },
      ],
    });
    expect(pill(items)).toMatchObject({ status: "waiting", resolved: false });
  });

  it("the server's waiting wins over a stale local running pill", () => {
    const local: Item[] = [{ kind: "swarm_pending", job_ids: [RUN], objective: "o", status: "running", resolved: false }];
    const remote: Item[] = [{ kind: "swarm_pending", job_ids: [RUN], objective: "o", status: "waiting", resolved: false }];
    expect(pill(mergeTranscriptItems(local, remote)).status).toBe("waiting");
  });

  it("renders a distinct waiting pill without a spinner", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "ship it" } },
      { kind: "swarm_pending", job_ids: [RUN], objective: "Flow g: ship", status: "waiting", resolved: false },
    ];
    render(
      <TranscriptList
        items={items}
        status="done"
        compactingStatus={null}
        editingIndex={null}
        auto={false}
        plan={false}
        turnOpen={false}
        scrollContainerRef={{ current: null }}
        onEditMessage={vi.fn()}
        onExecuteSend={vi.fn()}
        onImageClick={vi.fn()}
        onSetCard={vi.fn()}
        onExecutePlan={vi.fn()}
        onCommandApproval={vi.fn()}
      />,
    );
    const rendered = screen.getByTestId("swarm-pending-pill");
    expect(rendered.getAttribute("data-status")).toBe("waiting");
    expect(rendered.textContent).toContain("waiting for an answer: Flow g: ship");
    expect(rendered.querySelector(".animate-spin")).toBeNull();
  });
});
