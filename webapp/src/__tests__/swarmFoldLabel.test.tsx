import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearActivityFoldPrefs, collectIntermediateAssistantItems, foldSwarmSteps, groupAgentActivity, TranscriptList,
  type Item, type SwarmPendingItem,
} from "../components/TranscriptList";
import { applySwarmResultToItems, dropSwarmToolPrep } from "../components/conversation/streamApply";
import { upsertToolPrep } from "../components/conversation/thinkingToolPrep";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

const pill = (status: SwarmPendingItem["status"], id = "job_aaaaaaaaaaaa", extra: Partial<SwarmPendingItem> = {}): SwarmPendingItem => ({
  kind: "swarm_pending", job_ids: [id], objective: "audit calc", status, ...extra,
});

function renderList(items: Item[]) {
  return render(
    <TranscriptList items={items} status="done" compactingStatus={null} editingIndex={null} auto={false} plan={false}
      turnOpen={false} scrollContainerRef={{ current: null }} onEditMessage={vi.fn()} onExecuteSend={vi.fn()}
      onImageClick={vi.fn()} onSetCard={vi.fn()} onExecutePlan={vi.fn()} />,
  );
}

describe("foldSwarmSteps", () => {
  it("counts each swarm once, whichever rows carry it", () => {
    const card: Item = { kind: "card", card: { id: "call_1", kind: "run_swarm", goal: "audit", cwd: null, result: { job_id: "job_1" } } };
    const result: Item = { kind: "swarm_result", job_id: "job_1", applied: false, files: [], objective: "audit" };
    // Live: the run_swarm card and its pill.
    expect(foldSwarmSteps([card, pill("running", "local-swarm-call_1")] as never)).toHaveLength(0);
    // Live, as the rig stream sent it: a name-only tool_prep, then the
    // action start, then the pill. The prep row used to stay, so the fold
    // read "Running 2 swarms".
    let live: Item[] = upsertToolPrep([{ kind: "msg", msg: { role: "user", text: "go" } }], "run_swarm", {});
    live = dropSwarmToolPrep(live, { id: "call_bf5c", kind: "run_swarm" });
    live = [...live, pill("running", "local-swarm-call_bf5c")];
    expect(foldSwarmSteps(live.filter((it) => it.kind !== "msg") as never)).toHaveLength(1);
    expect(live.some((it) => it.kind === "card")).toBe(false);
    // Settled: the result and the pill that names the result's job.
    expect(foldSwarmSteps([pill("done", "local-swarm-call_1", { terminal_job_ids: ["job_1"] }), result] as never)).toHaveLength(1);
    // After a reload: only the result.
    expect(foldSwarmSteps([result] as never)).toHaveLength(1);
    expect(foldSwarmSteps([pill("done"), pill("failed", "job_bbbbbbbbbbbb")] as never)).toHaveLength(2);
  });
});

describe("live swarm with a steer", () => {
  it("counts the swarm once when its result lands after the steer", () => {
    // Live order in the rig: the pill, the steer bubble, then the result.
    // The pill named only its local id, so the fold before the steer and
    // the fold after it each counted a swarm.
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit" } },
      { kind: "thinking", text: "dispatch" } as Item,
      pill("running", "local-swarm-call_bf5c"),
      { kind: "steer", text: "Also check negative numbers." },
    ];
    items = applySwarmResultToItems(items, { job_id: "job_8f7a28ad721d", objective: "audit calc", summary: "2 findings" });
    items = [...items, { kind: "thinking", text: "read the files" } as Item];
    const groups = groupAgentActivity(items, collectIntermediateAssistantItems(items, false));
    const swarms = groups.flatMap((g) => (g.kind === "activity_group" ? foldSwarmSteps(g.items) : []));
    expect(swarms).toHaveLength(1);
  });
});

describe("swarm fold label", () => {
  it("a finished swarm fold names the swarm, as it did while it ran", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit it" } },
      pill("done"),
      { kind: "msg", msg: { role: "assistant", text: "Done." } },
    ];
    renderList(items);
    expect(screen.queryByText(/pending/)).toBeNull();
    expect(screen.getByRole("button", { name: /Ran 1 swarm/ })).toBeInTheDocument();
  });

  it("a finished swarm in the live fold keeps the past verb while the pilot thinks", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit it" } },
      { kind: "steer", text: "Also check negative numbers." },
      { kind: "swarm_result", job_id: "job_1", applied: false, files: [], objective: "audit calc", summary: "2 findings" },
      { kind: "thinking", text: "Now check negatives.", streaming: true } as Item,
    ];
    render(
      <TranscriptList items={items} status="thinking" compactingStatus={null} editingIndex={null} auto={false} plan={false}
        turnOpen scrollContainerRef={{ current: null }} onEditMessage={vi.fn()} onExecuteSend={vi.fn()}
        onImageClick={vi.fn()} onSetCard={vi.fn()} onExecutePlan={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /Ran 1 swarm/ })).toBeInTheDocument();
  });

  it("a steer sent during a swarm keeps the swarm and the turn time after a reload", () => {
    // Display rows of the rig session e7227eb49abc (text cut short): the
    // swarm ran, the user steered, and the pilot worked on. Before the fix
    // the turn read "Worked for 7s" (timed from the steer), and the swarm
    // card hid in a fold named "Thought".
    const display = [
      { type: "message", role: "user", text: "Use run_swarm with exactly 2 workers to review src/calc.py", ts: 1794599065212 },
      { type: "thinking", text: "User asked for run_swarm with exactly 2 workers.", duration_ms: 440, ts: 1794599067545 },
      { type: "card", id: "call_fdc4", kind: "run_swarm", goal: "Review src/calc.py", result: { job_id: "job_9d5db3d24307", num: 14, error: null, duration_ms: 10159 }, call_id: "call_fdc4", ts: 1794599067547 },
      { type: "swarm_result", job_id: "job_9d5db3d24307", applied: true, files: [], summary: "9 findings via agentic (14 artifacts)", error: null, objective: "Review src/calc.py", adapter: "agentic", ts: 1794599077908 },
      { type: "message", role: "user", text: "Also check negative numbers.", steer: true, ts: 1794599077920 },
      { type: "thinking", text: "The swarm returned. Now the user says also check negatives.", duration_ms: 1045, ts: 1794599081105 },
      { type: "card", id: "call_3080", kind: "run_command", goal: "python -c 'from src.calc import add'", result: { num: 1, duration_ms: 121 }, call_id: "call_3080", ts: 1794599081106 },
      { type: "thinking", text: "Report. Negatives pass.", duration_ms: 717, ts: 1794599084978 },
      { type: "message", role: "assistant", text: "Two workers reviewed against the live tree.", ts: 1794599084979 },
    ];
    renderList(transcriptResponseToItems({ display }, "s1"));
    const turn = screen.getByTestId("turn-work-fold");
    expect(turn.textContent).toContain("Worked for 19s");
    fireEvent.click(within(turn).getAllByRole("button")[0]);
    const swarmFold = screen.getByRole("button", { name: /Ran 1 swarm/ });
    fireEvent.click(swarmFold);
    expect(screen.getByTestId("swarm-result-card")).toBeInTheDocument();
    expect(screen.getByText("Also check negative numbers.")).toBeInTheDocument();
  });
});
