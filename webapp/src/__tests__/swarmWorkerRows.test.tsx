import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SwarmWorkerRows } from "../components/conversation/SwarmWorkerRows";
import { TranscriptList, type Item } from "../components/TranscriptList";
import type { ExpertArtifact, ExpertTask } from "../lib/expertMetadata";
import type { MetadataSelection } from "../lib/jobMetadata";
import { resolveSwarmSelection, workerLastAction } from "../lib/swarmWorkers";
import { currentDetail, currentFacts } from "./expertCurrent.fixtures";
import { expertMetadataFixture, expertSummary } from "./metadataExpert.fixtures";

const REPO = "/workspace/swarm-rows";
const selection: MetadataSelection = {
  repo: REPO, session_id: "sess-rows", source: "harness",
  job_ref: { job_id: "job_abcdef012345", state_id: "state_fixture", version: 2, incarnation: "12345678-1234-4234-8234-123456789abc" },
};

let dispose: (() => void) | null = null;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = null;
});

async function fixture() {
  const metadata = await expertMetadataFixture([{ ...expertSummary(selection, "Audit calc"), lifecycle: "complete" }]);
  metadata.selected.mockImplementation(async () => ({
    ...currentDetail(currentFacts("deepseek/deepseek-v4.1-flash", "Found a gap"), selection),
    context: metadata.context(),
  }));
  dispose = () => metadata.dispose();
  return metadata;
}

/** Run the store's scheduler through its detail slot, as its ticks do in the app. */
async function schedule(metadata: Awaited<ReturnType<typeof fixture>>) {
  for (let i = 0; i < 16; i++) await act(async () => { await metadata.store.advance(); });
}

describe("inline swarm worker rows", () => {
  it("shows role, model, state, tokens, and last action from the job metadata store", async () => {
    const metadata = await fixture();
    render(<metadata.Provider><SwarmWorkerRows swarmKey={selection.job_ref.job_id} running={false} /></metadata.Provider>);
    await schedule(metadata);
    const [row] = await screen.findAllByTestId("swarm-worker-row");
    expect(row.textContent).toContain("Auditor");
    expect(row.textContent).toContain("deepseek-v4.1-flash");
    expect(row.textContent).toContain("running");
    expect(row.textContent).toContain("120 tok");
    expect(row.textContent).toContain("Regression checks");
    // The detail read goes to the store cache; the Jobs pane keeps its selection.
    expect(metadata.store.getSnapshot().detail.kind).toBe("none");
  });

  it("opens the Swarm Tracker on the row's job", async () => {
    const metadata = await fixture();
    const spy = vi.spyOn(window, "dispatchEvent");
    render(<metadata.Provider><SwarmWorkerRows swarmKey={selection.job_ref.job_id} running={false} /></metadata.Provider>);
    await schedule(metadata);
    fireEvent.click((await screen.findAllByTestId("swarm-worker-row"))[0]);
    const open = spy.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.type === "harness-open-swarm-job");
    expect(open?.detail.jobId).toBe(selection.job_ref.job_id);
    spy.mockRestore();
  });

  it("paints under a swarm result card in the transcript", async () => {
    const metadata = await fixture();
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit it" } },
      { kind: "swarm_result", job_id: selection.job_ref.job_id, applied: false, analysis_ok: true, files: [], summary: "1 finding", objective: "audit" },
      { kind: "msg", msg: { role: "assistant", text: "Done." } },
    ];
    render(
      <metadata.Provider>
        <TranscriptList items={items} status="done" compactingStatus={null} editingIndex={null} auto plan={false}
          turnOpen={false} scrollContainerRef={{ current: null }} onEditMessage={vi.fn()} onExecuteSend={vi.fn()}
          onImageClick={vi.fn()} onSetCard={vi.fn()} onExecutePlan={vi.fn()} />
      </metadata.Provider>,
    );
    const fold = document.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
    if (fold) fireEvent.click(fold);
    await schedule(metadata);
    expect(await screen.findAllByTestId("swarm-worker-row")).toHaveLength(1);
  });

  it("shows nothing for a swarm the store does not list", async () => {
    const metadata = await fixture();
    render(<metadata.Provider><SwarmWorkerRows swarmKey="job_000000000000" running={false} /></metadata.Provider>);
    await schedule(metadata);
    expect(screen.queryByTestId("swarm-worker-rows")).toBeNull();
  });
});

describe("resolveSwarmSelection", () => {
  const canonical = {
    source: "harness" as const, session_id: "sess-rows", dispatch_id: "call_d3de",
    job_ref: { job_id: "job_feed01234567", state_id: "st", version: 2 as const, incarnation: "12345678-1234-4234-8234-123456789abc" },
  };
  const state = {
    view: { kind: "view", target: { repo: REPO, session_id: "sess-rows" }, context: { repo: REPO, session_id: "sess-rows" } },
    observations: [],
    local: { observations: [{ row: { local_ref: { job_id: "local-swarm-call_d3de", incarnation: "x" }, canonical } }] },
  } as unknown as Parameters<typeof resolveSwarmSelection>[0];
  it.each(["local-swarm-call_d3de", "call_d3de", "job_feed01234567"])("maps %s to the canonical job", (key) => {
    expect(resolveSwarmSelection(state, key)?.job_ref.job_id).toBe("job_feed01234567");
  });
  it("has no job for an unknown id", () => {
    expect(resolveSwarmSelection(state, "local-swarm-other")).toBeNull();
  });
  it("has no job of another owner", () => {
    const other = { ...state, view: { kind: "view", target: { repo: "/other", session_id: "s2" }, context: { repo: "/other", session_id: "s2" } } } as typeof state;
    expect(resolveSwarmSelection(other, "call_d3de")).toBeNull();
  });
});

describe("workerLastAction", () => {
  const task = { id: "t1", role: "r", instruction: "Do X\nmore", usage: {} } as unknown as ExpertTask;
  const art = (patch: Partial<ExpertArtifact>) => ({ task_id: "t1", type: "finding", headline: "", failure: null, ...patch }) as ExpertArtifact;
  it("prefers a failure", () => {
    expect(workerLastAction(task, "failed", [art({ headline: "h" }), art({ failure: "timeout after 600s" })])).toBe("timeout after 600s");
  });
  it("skips routing rows and shows the task only while the worker runs", () => {
    const routing = [art({ type: "ROUTING", headline: "routed to x" })];
    expect(workerLastAction(task, "running", routing)).toBe("Do X");
    expect(workerLastAction(task, "complete", routing)).toBe("");
  });
});
