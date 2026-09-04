import { createRef } from "react";
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ComposerActivityRail from "../components/conversation/ComposerActivityRail";
import ComposerDock from "../components/conversation/ComposerDock";
import ComposerTasksPanel from "../components/conversation/ComposerTasksPanel";
import { COMPOSER_FAMILY_CLASS } from "../components/conversation/composerFamily";
import type { Job } from "../lib/api";
import { clearSessionTodos, publishSessionTodos } from "../lib/sessionTodos";

vi.mock("../lib/api", () => ({
  api: { swarmCancel: vi.fn(), getSessionState: vi.fn(async () => ({ todos: { phases: [] } })) },
}));
vi.mock("../components/PilotPicker", () => ({
  default: () => <div data-testid="pilot-picker" />,
}));
vi.mock("../components/SwarmReasoningPicker", () => ({
  default: () => <div data-testid="swarm-reasoning-picker" />,
}));
vi.mock("../components/conversation/WorkspaceChip", () => ({
  default: () => <div data-testid="workspace-chip" />,
}));
vi.mock("../lib/agentCommandIndex", () => ({
  subscribeAgentCommandIndex: (cb: () => void) => {
    void cb;
    return () => {};
  },
  getAgentCommandIndexVersion: () => 1,
  listAgentCommandSessions: () => [],
  registerAgentCommandSession: () => null,
  dismissAgentCommandSession: () => false,
}));

const noop = () => {};

function renderDock() {
  return render(
    <ComposerDock
      config={null}
      taRef={createRef<HTMLTextAreaElement>()}
      input=""
      auto={false}
      plan={false}
      composerBusy={false}
      transcriptStale={false}
      wikiPrepared={null}
      memoryProposals={[]}
      distillNotice={null}
      msgQueue={[]}
      dragIndex={null}
      dragOverIndex={null}
      queueItems={[]}
      queueDragIndex={null}
      queueDragOverIndex={null}
      editingIndex={null}
      canRevertEdit={false}
      editNotice={null}
      editBusy={false}
      showContextPanel={false}
      contextUsage={null}
      mentionSearch={null}
      filteredFiles={[]}
      filteredFolders={[]}
      symbolResults={[]}
      mentionListingCap={null}
      selectedFileIndex={0}
      codegraphStatus={null}
      slashSearch={null}
      selectedSlashIndex={0}
      allSlashCommands={[]}
      attachedImages={[]}
      isDragOver={false}
      uploadError={null}
      onSetWikiPrepared={noop}
      onSetMemoryProposals={noop}
      onSetDistillNotice={noop}
      onSetMsgQueue={noop}
      onSetInput={noop}
      onSetAuto={noop}
      onSetPlan={noop}
      onSetCanRevertEdit={noop}
      onSetEditNotice={noop}
      onSetShowContextPanel={noop}
      onSetSelectedFileIndex={noop}
      onSetSelectedSlashIndex={noop}
      onSetAttachedImages={noop}
      onSetUploadError={noop}
      onSetLightboxUrl={noop}
      setSafeTimeout={noop}
      fetchContextUsage={noop}
      handleDragStart={noop}
      handleDragOver={noop}
      handleDragLeave={noop}
      handleDrop={noop}
      handleDragEnd={noop}
      moveQueueItem={noop}
      handleQueueClearAll={noop}
      handleQueueDragStart={noop}
      handleQueueDragOver={noop}
      handleQueueDragLeave={noop}
      handleQueueDrop={noop}
      handleQueueDragEnd={noop}
      handleQueueEdit={noop}
      handleQueueRemove={noop}
      handleComposerDragOver={noop}
      handleComposerDragLeave={noop}
      handleComposerDrop={noop}
      handleRevertEdit={noop}
      handleCancelEdit={noop}
      handleInputChange={noop}
      handleKeyDown={noop}
      handlePaste={noop}
      insertMention={noop}
      insertFolder={noop}
      insertSymbol={noop}
      insertCodebase={noop}
      showCodebaseMention={false}
      insertSlashCommand={noop}
      handleQueueAdd={noop}
      stop={noop}
      send={noop}
    />,
  );
}

const taskJob = {
  id: "job_tasks",
  goal: "Ship composer-family chrome",
  status: "running",
  session_id: "sess-1",
  source: "harness",
  tasks: [
    { id: "t1", role: "impl", instruction: "Restyle trackers", status: "running", adapter: "x" },
  ],
} as Job;

const swarmJob = {
  id: "job_abc123def456",
  goal: "Audit composer stack",
  source: "harness",
  status: "running",
  session_id: "sess-1",
  updated_at: Date.now(),
} as Job;

describe("composer-family chrome", () => {
  afterEach(() => {
    clearSessionTodos();
  });

  it("puts the same class family and tokens on dock and the activity rail", () => {
    const dock = renderDock();
    const rail = render(
      <ComposerActivityRail jobs={[taskJob, swarmJob]} sessionId="sess-1" />,
    );

    const surfaces = [
      dock.container.querySelector(".composer-dock"),
      rail.container.querySelector("[data-slot=composer-activity-rail]"),
    ];

    for (const el of surfaces) {
      expect(el).toBeTruthy();
      expect(el).toHaveClass(COMPOSER_FAMILY_CLASS);
      expect(el).toHaveClass("bg-panel2/80");
      expect(el).toHaveClass("rounded-2xl");
      expect(el).toHaveClass("border-edge");
      expect(el).toHaveClass("shadow-lg");
      expect(el?.className).not.toMatch(/text-muted-foreground|rose-500|uppercase tracking-\[0\.16em\]/);
    }

    const railSurface = rail.container.querySelector("[data-slot=composer-activity-rail]");
    expect(railSurface).not.toHaveClass("mx-2");
    expect(railSurface?.firstElementChild).not.toHaveClass("divide-y");

    const tasks = rail.container.querySelector("[data-slot=composer-tasks-panel]");
    const stack = rail.container.querySelector("[data-slot=composer-status-stack]");
    expect(tasks).toHaveClass(COMPOSER_FAMILY_CLASS);
    expect(stack).toHaveClass(COMPOSER_FAMILY_CLASS);
    expect(tasks?.className).not.toMatch(/border-edge|rounded-2xl|bg-panel2/);
    expect(stack?.className).not.toMatch(/border-edge|rounded-2xl|bg-panel2/);
  });

  it("does not paint an empty activity rail", () => {
    const rail = render(
      <ComposerActivityRail jobs={[]} sessionId="sess-1" />,
    );
    expect(rail.container.querySelector("[data-slot=composer-activity-rail]")).toBeNull();
  });

  it("renders a nested session TODO tree on the activity rail", () => {
    publishSessionTodos({
      phases: [
        {
          name: "WP-02",
          tasks: [
            { content: "hosted pari", status: "in_progress" },
            { content: "service invariants", status: "pending" },
          ],
        },
      ],
      next: "hosted pari",
    }, "sess-1");
    const rail = render(
      <ComposerActivityRail jobs={[]} sessionId="sess-1" />,
    );
    expect(rail.container.querySelector("[data-slot=composer-todo-panel]")).toBeTruthy();
    expect(rail.getByText("TODO 0/2")).toBeInTheDocument();
    expect(rail.getByText(/I\. WP-02 · 0\/2/)).toBeInTheDocument();
    clearSessionTodos();
  });

  it("does not paint another session's TODO tree on this conversation", () => {
    publishSessionTodos({
      phases: [
        {
          name: "Wave I",
          tasks: [{ content: "beyblade leftover", status: "in_progress" }],
        },
      ],
    }, "sess-arena");
    const rail = render(
      <ComposerActivityRail jobs={[]} sessionId="sess-marionette" />,
    );
    expect(rail.container.querySelector("[data-slot=composer-activity-rail]")).toBeNull();
    expect(rail.queryByText("beyblade leftover")).toBeNull();
    clearSessionTodos();
  });

  it("lights a pending session TODO from a live swarm label", () => {
    publishSessionTodos({
      phases: [
        {
          name: "WP-02",
          tasks: [
            { content: "hosted pari", status: "pending" },
            { content: "unrelated cutover", status: "pending" },
          ],
        },
      ],
    }, "sess-1");
    const job = {
      id: "j-live",
      goal: "nested lighting",
      status: "running",
      session_id: "sess-1",
      tasks: [{ id: "t1", role: "explore", instruction: "hosted pari service", status: "running" }],
    } as Job;
    const rail = render(
      <ComposerActivityRail jobs={[job]} sessionId="sess-1" />,
    );
    const lit = rail.container.querySelector("[data-todo-lit='1']");
    expect(lit).toBeTruthy();
    expect(lit?.textContent).toContain("hosted pari");
    clearSessionTodos();
  });

  it("renders parallel wave partial header from child counts", () => {
    const waveTasks = Array.from({ length: 8 }, (_, i) => ({
      id: `c${i}`,
      role: "implement",
      instruction: `goal ${i}`,
      status: i < 4 ? "completed" : "failed",
      adapter: "x",
      applied: i < 4,
    }));
    const wave = {
      id: "local-wave-mix",
      goal: "mixed implement",
      status: "partial",
      session_id: "sess-1",
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_count: 8,
      review_required: true,
      tasks: waveTasks,
    } as Job;
    const { getByText } = render(
      <ComposerTasksPanel jobs={[wave]} sessionId="sess-1" />,
    );
    expect(
      getByText("Parallel wave — partial 4/8 completed · 4 failed · 4 patches applied · review required"),
    ).toBeInTheDocument();
  });

  it("does not repeat parallel-wave children in the Puppetmaster group", () => {
    const childIds = ["local-60b0773c", "local-42c84d72", "local-f25dd421"];
    const wave = {
      id: "local-wave-call_3Du6fz",
      goal: "Wave 2B",
      status: "running",
      session_id: "sess-1",
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_job_ids: childIds,
      tasks: childIds.map((id, index) => ({
        id,
        role: "implement",
        instruction: `slice ${index}`,
        status: index === 0 ? "running" : index === 1 ? "completed" : "failed",
        adapter: "agentic",
      })),
    } as Job;
    const children = childIds.map((id, index) => ({
      id,
      goal: `slice ${index}`,
      status: index === 0 ? "running" : index === 1 ? "completed" : "failed",
      session_id: "sess-1",
      job_kind: "run_implement",
      source: "harness",
      updated_at: Date.now(),
    } as Job));
    const unrelated = {
      id: "job_unrelated",
      goal: "Independent audit",
      status: "running",
      session_id: "sess-1",
      job_kind: "run_swarm",
      source: "harness",
      updated_at: Date.now(),
    } as Job;

    const rail = render(
      <ComposerActivityRail jobs={[wave, ...children, unrelated]} sessionId="sess-1" />,
    );

    expect(rail.getByText(/Parallel wave — running/)).toBeInTheDocument();
    expect(rail.getByRole("button", { name: "Puppetmaster" })).toHaveTextContent("1");
    expect(rail.queryByRole("button", { name: "Open swarm: slice 0" })).not.toBeInTheDocument();
    expect(rail.getByText("Independent audit")).toBeInTheDocument();
  });

  it("unmounts a completed 5/5 parallel wave instead of leaving implement flags", () => {
    const wave = {
      id: "local-wave-done",
      goal: "implement",
      status: "completed",
      session_id: "sess-1",
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_count: 5,
      tasks: Array.from({ length: 5 }, (_, i) => ({
        id: `c${i}`,
        role: "implement",
        instruction: "implement",
        status: "completed",
        adapter: "x",
      })),
    } as Job;
    const { container, queryByText } = render(
      <ComposerTasksPanel jobs={[wave]} sessionId="sess-1" />,
    );
    expect(container.querySelector("[data-slot=composer-tasks-panel]")).toBeNull();
    expect(queryByText("Parallel wave — completed 5/5 completed")).not.toBeInTheDocument();
    expect(queryByText("implement")).not.toBeInTheDocument();
  });

  it("unmounts a settled partial wave while an unrelated swarm is running", () => {
    const wave = {
      id: "local-wave-stale",
      goal: "implement",
      status: "partial",
      session_id: "sess-1",
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_count: 4,
      tasks: [
        { id: "c1", role: "implement", instruction: "implement", status: "completed", adapter: "x" },
        { id: "c2", role: "implement", instruction: "implement", status: "failed", adapter: "x" },
        { id: "c3", role: "implement", instruction: "implement", status: "completed", adapter: "x" },
        { id: "c4", role: "implement", instruction: "implement", status: "completed", adapter: "x" },
      ],
    } as Job;
    const swarm = {
      id: "job_live_swarm",
      goal: "Perform a fresh read-only validation",
      status: "running",
      session_id: "sess-1",
      job_kind: "run_swarm",
      tasks: [
        { id: "w1", role: "decision-explainer", instruction: "Explain", status: "running", adapter: "agentic" },
        { id: "w2", role: "explore", instruction: "Explore", status: "running", adapter: "agentic" },
      ],
    } as Job;
    const { container, queryByText, getByText } = render(
      <ComposerTasksPanel jobs={[wave, swarm]} sessionId="sess-1" />,
    );
    expect(queryByText("Parallel wave — partial 3/4 completed · 1 failed")).not.toBeInTheDocument();
    expect(container.querySelector("[data-slot=composer-tasks-panel]")).toBeTruthy();
    expect(getByText("Tasks 0/2")).toBeInTheDocument();
  });
});
