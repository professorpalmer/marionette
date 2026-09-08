import { economicsNativeFixture, economicsReceiptFixture, nativeEconomicsRow } from './frontend52-economics.fixtures';
import { identityFixture, identityTask, workerDetails } from './frontend52-identity.fixtures';
import { capturedRoutingFixture, referenceHash, routingReference } from './frontend52-routing.fixtures';
import { evidenceFixture, evidenceHash, evidenceSelection, qualityFixture, rejectQualityVerdicts } from './frontend52-evidence.fixtures';
import { MetadataInspection } from '../components/MetadataJobs';
import { parseMetadataDetail } from '../lib/jobMetadata';
import { nativeExpertFixture, capturedModelDetail } from './nativeExpert.fixtures';
import { act, fireEvent, render, screen, waitFor, cleanup, within } from "@testing-library/react";

import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import SwarmPane, { jobDegradedWorkerCount, jobIdentifier, jobSavings, namedSavings, workerSpend, workerOutcome } from "../components/SwarmPane";

import { api, type Job, type SwarmLive } from "../lib/api";

import { dispatchProjectSelected } from "../lib/panelTransition";

import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

import { jobControlKey } from "../lib/jobControl";

import { fetchJobArtifacts } from "../lib/jobArtifacts";

import { expertMetadataFixture, expertSummary, expertDetail } from "./metadataExpert.fixtures";

import type { MetadataSummary, MetadataContext, MetadataSelection } from "../lib/jobMetadata";

import { swarmCacheFixture } from "./swarmCache.fixtures";

import { view as metadataView, view, selection, selection as metadataSelection } from "./jobMetadata.fixtures";

import { metadataSelectionKey } from "../lib/jobMetadata";

import { queuePendingSwarmNavigation, peekPendingSwarmNavigation } from "../lib/pendingSwarmOpenJob";

import { terminalWorkerMetadataFixture } from "./workerIdentity.fixtures";

import { nativeControlFixture, outcomeSummary } from "./outcomeControls.fixtures";

import { renderWorkerMetadata } from "./workerOutcomes.fixtures";

import type { SelectedMetric } from "../lib/selectedMetadataEvidence";

import { commandSplitFixture } from "./commandSplit.fixtures";

function expansionSummary(id: string, goal: string, repo = '/repo', lifecycle = 'running'): MetadataSummary {
  return { ...expertSummary({ ...selection(), repo, job_ref: { ...selection().job_ref, job_id: id,
    version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } }, goal), lifecycle };
}

function expansionStorageKey(repo = '/repo') {
  return `pmharness.metadata.jobs:${JSON.stringify([repo, selection().session_id])}`;
}

vi.mock("../lib/jobArtifacts", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/jobArtifacts")>(), fetchJobArtifacts: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      swarmLive: vi.fn(),
      swarmCancel: vi.fn(),
      artifacts: vi.fn(),
      sessions: vi.fn().mockResolvedValue([]),
    },
  };
});

const mockSwarmLive = vi.mocked(api.swarmLive);

const mockSwarmCancel = vi.mocked(api.swarmCancel);

const mockArtifacts = vi.mocked(api.artifacts);

function savedKey(id: string, repo: string, sessionId = "", owner = "sess-test", source = "harness", stateId: string | null = null) {
  return jobControlKey({ id, session_id: owner, source,
    ...(stateId ? { job_ref: { job_id: id, state_id: stateId } } : {}) }, repo, sessionId);
}

function liveJob(
  jobOverrides: Partial<SwarmLive["jobs"][number]> = {},
  sessionOverrides: Partial<SwarmLive["session"]> = {},
): SwarmLive {
  return {
    session: { tokens_used: 0, est_cost_usd: 0, ...sessionOverrides },
    jobs: [
      {
        id: "job-1",
        goal: "Audit auth flow",
        status: "running",
        session_id: "sess-test",
        ...jobOverrides,
      },
    ],
  };
}

function finishedJob(
  id: string,
  goal: string,
  overrides: Partial<SwarmLive["jobs"][number]> = {},
): SwarmLive {
  return liveJob({ id, goal, status: "complete", adapter: "agentic", ...overrides });
}

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

async function expandJob(name: string | RegExp) {
  const job = await screen.findByRole("button", { name });
  if (job.getAttribute("aria-expanded") === "false") {
    fireEvent.click(job);
  }
  return job;
}

async function expandVisibleJobs() {
  await waitFor(() => {
    expect(screen.queryByText("Loading swarm jobs...")).not.toBeInTheDocument();
  });
  for (const btn of screen.getAllByRole("button")) {
    if (btn.getAttribute("aria-expanded") === "false" && (btn.getAttribute("aria-label") || "").trim()) {
      fireEvent.click(btn);
    }
  }
}

describe("SwarmPane sort and filter controls", () => {
  let metadata: Awaited<ReturnType<typeof expertMetadataFixture>>;
  beforeEach(async () => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    const rows: MetadataSummary[] = [
      ["job-z", "Older active audit", "running"],
      ["job-a", "Newest active build", "running"],
      ["job-no-time", "Active job without timestamp", "running"],
      ["job-y", "Older completed review", "complete"],
      ["job-b", "Newest failed review", "failed"],
      ["job-c", "Degraded architecture review", "complete"],
    ].map(([id, goal, lifecycle]) => ({
      ...expertSummary({ repo: "/repo", session_id: "sess-test", source: "harness",
        job_ref: { job_id: id.replace("job-", "job_"), state_id: "store-A", version: 2,
          incarnation: "12345678-1234-4234-8234-123456789abc" } }, goal),
      lifecycle,
    }));
    metadata = await expertMetadataFixture(rows, { browser: true });
  });
  afterEach(() => {
    cleanup();
    metadata?.dispose();
    vi.unstubAllGlobals();
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("distributes filter and sort controls evenly across the toolbar", async () => {
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    const filter = await screen.findByLabelText("Filter swarms");
    const sort = screen.getByLabelText("Sort swarms");

    expect(filter.parentElement).toHaveClass("grid", "grid-cols-2");
    expect(filter).toHaveClass("w-full");
    expect(sort).toHaveClass("w-full");
  });

  it("sorts dated native jobs newest-first ahead of undated observations", async () => {
    const native = await economicsNativeFixture([
      nativeEconomicsRow('local-z-new', 'running', 200), nativeEconomicsRow('local-a-old', 'running', 100),
      nativeEconomicsRow('local-b-undated', 'running', null), nativeEconomicsRow('local-z-finished-new', 'failed', 200),
      nativeEconomicsRow('local-a-finished-old', 'completed', 100),
    ]);
    const mounted = render(<native.Provider><SwarmPane /></native.Provider>);
    try {
      const row = (id: string) => screen.getByRole('button', { name: new RegExp(`^Provider worker · ${id} ·`) });
      expectBefore(row('local-z-new'), row('local-a-old'));
      expectBefore(row('local-a-old'), row('local-b-undated'));
      expectBefore(row('local-z-finished-new'), row('local-a-finished-old'));
      expectBefore(row('local-a-old'), screen.getByRole('button', { name: /^Undated PM observation ·/ }));
      expectBefore(row('local-z-new'), screen.getByRole('button', { name: /^Undated PM observation ·/ }));
      expect(screen.getByText(/PM creation times are unavailable/)).toBeVisible();
    } finally { mounted.unmount(); native.dispose(); }
  });

  it("reverses dated native jobs in both lifecycle groups when Oldest is selected", async () => {
    const native = await economicsNativeFixture([
      nativeEconomicsRow('local-z-new', 'running', 200), nativeEconomicsRow('local-a-old', 'running', 100),
      nativeEconomicsRow('local-b-undated', 'running', null), nativeEconomicsRow('local-z-finished-new', 'failed', 200),
      nativeEconomicsRow('local-a-finished-old', 'completed', 100),
    ]);
    const mounted = render(<native.Provider><SwarmPane /></native.Provider>);
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Sort swarms' }));
      expect(screen.getByRole('button', { name: 'Sort swarms' })).toHaveTextContent('Oldest first');
      const row = (id: string) => screen.getByRole('button', { name: new RegExp(`^Provider worker · ${id} ·`) });
      expectBefore(row('local-a-old'), row('local-z-new'));
      expectBefore(row('local-z-new'), row('local-b-undated'));
      expectBefore(row('local-a-finished-old'), row('local-z-finished-new'));
      expectBefore(row('local-a-old'), screen.getByRole('button', { name: /^Undated PM observation ·/ }));
      expectBefore(row('local-z-new'), screen.getByRole('button', { name: /^Undated PM observation ·/ }));
      expect(screen.getByText(/PM creation times are unavailable/)).toBeVisible();
    } finally { mounted.unmount(); native.dispose(); }
  });

  it("filters lifecycle without treating verification quality as known", async () => {
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    fireEvent.change(screen.getByLabelText('Filter swarms'), { target: { value: 'failed' } });
    expect(screen.getByRole('button', { name: /^Newest failed review ·/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Degraded architecture review ·/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Newest active build ·/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Filter swarms'), { target: { value: 'complete' } });
    expect(screen.getByRole('button', { name: /^Degraded architecture review ·/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /^Older completed review ·/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Newest failed review ·/ })).not.toBeInTheDocument();
    expect(screen.getByText('Completed lifecycle does not establish successful verification.')).toBeVisible();
    fireEvent.change(screen.getByLabelText('Filter swarms'), { target: { value: 'untrustworthy' } });
    expect(screen.getByText(/Quality cannot be assessed/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Degraded architecture review ·/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear filter' }));
    expect(screen.getByRole('button', { name: /^Newest failed review ·/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /^Degraded architecture review ·/ })).toBeVisible();
  });

  it("clears a filter with no matching jobs", async () => {
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    await screen.findByRole("button", { name: /^Newest active build ·/ });

    fireEvent.change(screen.getByLabelText("Filter swarms"), { target: { value: "cancelled" } });
    expect(await screen.findByText("No jobs observed in this filter. Coverage may be incomplete.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByRole("button", { name: /^Newest active build ·/ })).toBeInTheDocument();
  });
});

describe("SwarmPane SWR cache first-open", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\warm-swarm";
  let metadata: ReturnType<typeof swarmCacheFixture>;
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    metadata = swarmCacheFixture(REPO);
  });
  afterEach(async () => { cleanup(); vi.useRealTimers(); await metadata.settle(); metadata.dispose(); });

  it("starts only one request on mount and coalesces a slow first poll", async () => {
    vi.useFakeTimers();
    metadata.hangView();
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    await act(async () => {
      metadata.store.startTicks(2000);
      void metadata.store.ownerTick();
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(metadata.readView).toHaveBeenCalledTimes(1);
    expect(mockSwarmLive).not.toHaveBeenCalled();
  });

  it("starts a separate read when the active session changes during a poll", async () => {
    let finish: (value: unknown) => void = () => {};
    metadata.readView.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    let pending: Promise<void>;
    await act(async () => { pending = metadata.store.ownerTick(); });
    expect(metadata.readView).toHaveBeenCalledTimes(1);
    await act(async () => {
      window.dispatchEvent(new CustomEvent('harness-session-changed', { detail: { sessionId: 'next-session' } }));
      metadata.target(REPO, 'next-session', 'Next session job');
      // The shared owner serializes requests, then admits the new context.
      await metadata.store.ownerTick();
      finish({ ...metadataView(), context: metadata.readView.mock.calls[0][0] });
      await pending;
      await metadata.store.ownerTick();
    });
    expect(metadata.readView).toHaveBeenCalledTimes(2);
    expect(metadata.readView.mock.calls[1][0].session_id).toBe('next-session');
    expect(await screen.findByRole('button', { name: /Next session job/ })).toBeInTheDocument();
  });

  it("does no hidden-pane polling and refreshes when enabled", async () => {
    vi.useFakeTimers();
    const rendered = render(<metadata.Provider><SwarmPane enabled={false} /></metadata.Provider>);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(metadata.fetch).not.toHaveBeenCalled();
    rendered.rerender(<metadata.Provider><SwarmPane enabled /></metadata.Provider>);
    await act(async () => { await metadata.store.ownerTick(); });
    expect(metadata.readView).toHaveBeenCalledTimes(1);
    expect(metadata.store.getSnapshot().error).toBeNull();
    expect(screen.getByRole('button', { name: /Initial A/ })).toBeInTheDocument();
    expect(mockSwarmLive).not.toHaveBeenCalled();
  });

  it("owns initial session cache and rejects a late previous-project reply", async () => {
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    await act(async () => { await metadata.store.ownerTick(); });
    expect(screen.getByRole('button', { name: /Initial A/ })).toBeInTheDocument();
    expect(metadata.store.getSnapshot().observations[0].row.selection.session_id).toBe('A');
    let finish: (value: unknown) => void = () => {};
    let oldContext: MetadataContext | undefined;
    metadata.readView.mockImplementationOnce(captured => {
      oldContext = captured;
      return new Promise(resolve => { finish = resolve; });
    });
    let pending: Promise<void>;
    await act(async () => {
      metadata.target(REPO, 'old-pending', 'Late old project');
      pending = metadata.store.ownerTick();
    });
    await act(async () => {
      dispatchProjectSelected('/new-project');
      metadata.target('/new-project', 'B', 'Project B');
      finish({ ...metadataView(), context: oldContext });
      await pending;
      await metadata.store.ownerTick();
    });
    expect(screen.queryByText(/Late old project/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Project B/ })).toBeInTheDocument();
    expect(metadata.store.getSnapshot().observations.map(o => o.row.selection)).toEqual([
      expect.objectContaining({ repo: '/new-project', session_id: 'B' }),
    ]);
  });

  it("renders seeded jobs immediately without Loading swarm jobs...", async () => {
    metadata.target(REPO, 'A', 'Pre-warmed swarm job');
    await act(async () => { await metadata.store.ownerTick(); });
    const requests = metadata.fetch.mock.calls.length;
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    expect(screen.queryByText('Loading swarm jobs...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Pre-warmed swarm job/ })).toBeInTheDocument();
    expect(metadata.fetch).toHaveBeenCalledTimes(requests);
  });

  it("shows Loading swarm jobs... on cold mount with empty cache", async () => {
    metadata.hangView();
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    await act(async () => { void metadata.store.ownerTick(); });
    expect(screen.getByText('Loading swarm jobs...')).toBeInTheDocument();
    expect(screen.queryByText(/Pre-warmed swarm job/)).not.toBeInTheDocument();
  });
});

describe("SwarmPane model badge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockSwarmLive.mockResolvedValue(liveJob());
    mockArtifacts.mockResolvedValue([]);
  });

  it("discloses a captured attempt model without assigning it to the job", async () => {
    const fixture = await identityFixture({ models: ['anthropic/claude-sonnet-4'] });
    try {
      expect(fixture.job).not.toHaveTextContent('anthropic/claude-sonnet-4');
      fireEvent.click(screen.getByRole('button', { name: 'Routing', exact: true }));
      const routing = screen.getByRole('region', { name: 'Routing' });
      expect(within(routing).getByTitle('Model: anthropic/claude-sonnet-4')).toBeVisible();
      expect(routing).toHaveTextContent('Historical model; current job and worker model unconfirmed.');
      expect(routing).toHaveTextContent('Task: task-a. Run: run-0.');
    } finally { fixture.close(); }
  });

  it("marks only worker, job, and aggregate running indicators as semantic", async () => {
    const selected = selection();
    const fixture = await expertMetadataFixture([expertSummary(selected, "Audit auth flow")]);
    const view = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      const job = await expandJob(/Audit auth flow/);
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
      const worker = await screen.findByRole("button", { name: "task-1: running" });
      const aggregate = screen.getByText("1 running");

      expect(worker?.querySelector(".semantic-activity-spinner")).toBeTruthy();
      expect(job.querySelector(".semantic-activity-spinner")).toBeTruthy();
      expect(aggregate.querySelector(".semantic-activity-spinner")).toBeTruthy();
      expect(view.container.querySelectorAll(".semantic-activity-spinner")).toHaveLength(3);
      const headerPulse = screen.getByTitle("1 running");
      expect(headerPulse).toHaveClass("animate-pulse");
      expect(headerPulse).not.toHaveClass("semantic-activity-spinner");
      for (const pulse of view.container.querySelectorAll(".animate-pulse")) {
        expect(pulse).not.toHaveClass("semantic-activity-spinner");
      }
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
      await act(async () => { fixture.store.invalidate(); });
      expect(view.container.querySelectorAll(".semantic-activity-spinner")).toHaveLength(0);
    } finally { view.unmount(); fixture.dispose(); }
  });

  it("does not mark finished-job chrome as semantic activity", async () => {
    const selected = selection();
    const fixture = await expertMetadataFixture([{ ...expertSummary(selected, "Audit auth flow"), lifecycle: "complete" }]);
    fixture.selected.mockImplementation(async () => {
      const detail = expertDetail(selected, fixture.context());
      return { ...detail, lifecycle: "complete", tasks: { ...detail.tasks,
        rows: detail.tasks.rows.map(task => ({ ...task, status: "complete" })) } };
    });
    const view = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      expect(screen.getByRole("button", { name: "Finished (1 observed)" })).toHaveAttribute("aria-expanded", "true");
      await expandJob(/^Audit auth flow · complete$/);
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
      await screen.findByText("task-1: complete");
      expect(view.container.querySelectorAll(".semantic-activity-spinner")).toHaveLength(0);
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally { view.unmount(); fixture.dispose(); }
  });

  it("discloses captured adapter evidence when the attempt has no model", async () => {
    const fixture = await identityFixture({ models: [null] });
    try {
      const { button, details } = workerDetails('task-a');
      expect(details).not.toBeVisible();
      fireEvent.click(button);
      expect(details).toHaveTextContent('Captured adapteropenrouter');
      expect(details).toHaveTextContent('Historical identity; current worker model unconfirmed.');
      expect(within(details).queryByText('Captured model')).not.toBeInTheDocument();
      expect(button).not.toHaveTextContent('openrouter');
      expect(fixture.job).not.toHaveTextContent('openrouter');
    } finally { fixture.close(); }
  });

  it("shows task activity and binding without inferring model routing", async () => {
    const fixture = await identityFixture({ models: [] });
    try {
      const { button, details } = workerDetails('task-a');
      fireEvent.click(button);
      expect(details).toHaveTextContent('Generation3');
      expect(details).toHaveTextContent('Leaselease-task-a');
      expect(details).toHaveTextContent('Ownerowner-task-a');
      expect(button.querySelector('.semantic-activity-spinner')).toBeTruthy();
      expect(button).not.toHaveTextContent('routing');
      expect(fixture.job).not.toHaveTextContent('routing');
    } finally { fixture.close(); }
  });

  it("keeps captured models isolated by task and collapsed disclosure", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('task-a'), identityTask('task-b'), identityTask('task-unknown')], models: ['model-a', 'model-b'] });
    try {
      const a = workerDetails('task-a'), b = workerDetails('task-b'), unknown = workerDetails('task-unknown');
      fireEvent.click(a.button); fireEvent.click(b.button); fireEvent.click(unknown.button);
      expect(a.details).toHaveTextContent('Captured modelmodel-a');
      expect(a.details).not.toHaveTextContent('model-b');
      expect(b.details).toHaveTextContent('Captured modelmodel-b');
      expect(b.details).not.toHaveTextContent('model-a');
      expect(unknown.details).toHaveTextContent('Ownerowner-task-unknown');
      expect(unknown.details).not.toHaveTextContent('Captured model');
      expect(fixture.job).not.toHaveTextContent('model-a');
      fireEvent.click(a.button);
      expect(a.details).not.toBeVisible();
      expect(b.details).toBeVisible();
    } finally { fixture.close(); }
  });

  it("refreshes a disclosed captured model through explicit inspection", async () => {
    const fixture = await identityFixture({ models: [] });
    try {
      const { button, details } = workerDetails('task-a');
      fireEvent.click(button);
      expect(details).toHaveTextContent('Ownerowner-task-a');
      expect(details).not.toHaveTextContent('routed-model');
      fixture.models(['routed-model']);
      fireEvent.click(fixture.inspect);
      await waitFor(() => expect(details).toHaveTextContent('Captured modelrouted-model'));
      expect(details).toHaveTextContent('Historical identity; current worker model unconfirmed.');
      expect(button).not.toHaveTextContent('routed-model');
      expect(fixture.selected).toHaveBeenCalledTimes(2);
    } finally { fixture.close(); }
  });

  it("shows terminal run evidence without inferring complete model history", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('task-terminal', 'complete')], lifecycle: 'complete', models: [] });
    try {
      const { button, details } = workerDetails('task-terminal', 'complete');
      fireEvent.click(button);
      expect(details).toHaveTextContent('worker-task-terminal');
      expect(details).toHaveTextContent('completed at2026-09-07T19:00:00Z');
      expect(button).not.toHaveTextContent('routing');
      expect(button.querySelector('.semantic-activity-spinner')).toBeNull();
      expect(screen.queryByLabelText('No model recorded')).not.toBeInTheDocument();
      expect(details).toHaveTextContent('Historical identity; not current worker presentation.');
    } finally { fixture.close(); }
  });

  it("shows queued and pending bindings without inferring routing state", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('task-queued', 'queued'), identityTask('task-pending', 'pending')], models: [] });
    try {
      for (const [id, status] of [['task-queued', 'queued'], ['task-pending', 'pending']]) {
        const { button, details } = workerDetails(id, status);
        expect(button).toHaveAccessibleName(`${id}: ${status}`);
        fireEvent.click(button);
        expect(details).toHaveTextContent(`Ownerowner-${id}`);
        expect(details).toHaveTextContent(`Leaselease-${id}`);
        expect(button).not.toHaveTextContent('routing');
        expect(button).not.toHaveTextContent('no-model');
        expect(button.querySelector('.semantic-activity-spinner')).toBeNull();
      }
    } finally { fixture.close(); }
  });

  it("shows empty task coverage and job activity without inferring routing", async () => {
    const fixture = await identityFixture({ tasks: [], models: [] });
    try {
      const tasks = screen.getByRole('region', { name: 'Tasks' });
      expect(tasks).toHaveTextContent('0 tasks shown of 0. Page: complete.');
      expect(tasks).toHaveTextContent('No tasks recorded.');
      expect(fixture.job).toHaveTextContent('running');
      expect(fixture.job.querySelector('.semantic-activity-spinner')).toBeTruthy();
      expect(within(tasks).getByRole('button', { name: 'Next tasks' })).toBeDisabled();
      expect(screen.queryByTitle('Model routing in progress')).not.toBeInTheDocument();
    } finally { fixture.close(); }
  });
});

describe("SwarmPane worker details", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("lets a terminal worker reveal its source-backed identity", async () => {
    const fixture = await terminalWorkerMetadataFixture();
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      await expandJob(/^Inspect completed worker/);
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));

      const worker = await screen.findByRole("button", { name: /task-terminal: complete/ });
      expect(worker).toHaveAttribute("aria-expanded", "false");
      expect(screen.getByText("2026-08-16T17:00:00+00:00")).not.toBeVisible();
      fireEvent.click(worker);

      expect(worker).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByText("task-terminal", { exact: true })).toBeVisible();
      expect(screen.getByText("completed-worker", { exact: true })).toBeVisible();
      expect(screen.getByText("2026-08-16T17:00:00+00:00")).toBeVisible();
      expect(screen.queryByText("unrelated-worker")).not.toBeInTheDocument();
      expect(screen.queryByText("2026-08-15T12:00:00+00:00")).not.toBeInTheDocument();
      expect(worker.contains(document.getElementById(worker.getAttribute("aria-controls") || ""))).toBe(false);
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
    } finally {
      fixture.dispose();
    }
  });

  it("isolates captured process outcomes by task and disclosure", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('task-a', 'failed'), identityTask('task-b', 'failed')], lifecycle: 'failed', models: [],
      outcomes: [{ task: 'task-a', code: 17, timedOut: false }, { task: 'task-b', code: 23, timedOut: true }, { task: null, code: 99, timedOut: false }] });
    try {
      const a = workerDetails('task-a', 'failed'), b = workerDetails('task-b', 'failed');
      expect(a.details).not.toBeVisible();
      fireEvent.click(a.button);
      expect(a.details).toHaveTextContent('returncode17');
      expect(a.details).toHaveTextContent('timed outfalse');
      expect(a.details).toHaveTextContent('completed at2026-09-07T19:00:00Z');
      expect(a.details).not.toHaveTextContent('returncode23');
      expect(a.details).not.toHaveTextContent('returncode99');
      expect(b.details).not.toBeVisible();
      fireEvent.click(b.button);
      expect(b.details).toHaveTextContent('returncode23');
      expect(b.details).toHaveTextContent('timed outtrue');
      expect(b.details).not.toHaveTextContent('returncode17');
      expect(b.details).not.toHaveTextContent('returncode99');
      expect(b.details).toHaveTextContent('Historical process evidence; not a failure diagnosis.');
      fireEvent.click(a.button);
      expect(a.details).not.toBeVisible();
      expect(b.details).toBeVisible();
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally { fixture.close(); }
  });

  it("expands a worker from the keyboard and keeps nested Kill from toggling the job", async () => {
    dispatchProjectSelected("/cancel-repo");
    vi.mocked(api.sessions).mockResolvedValue([{ id: "sess-test", active: true }]);
    const metadata = await nativeExpertFixture({ jobId: 'local-keys', repo: '/cancel-repo',
      task: { role: 'key-worker', instruction: 'Keyboard disclosure', model: 'routed-model', model_kind: 'assigned' } });
    outcomeFixture = metadata;
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    mockSwarmCancel.mockResolvedValue({ ok: true, job_id: "local-keys" });
    expect(screen.queryByText("Keyboard disclosure")).not.toBeInTheDocument();
    expect(metadata.request.mock.calls.some(([, path]) => path.includes('/local/detail'))).toBe(false);
    const job = await expandJob(/Provider worker/);
    expect(job).toHaveAttribute("aria-expanded", "true");
    expect(job.className).toMatch(/focus-visible:outline/);
    expect(job.getAttribute("aria-label") || "").toMatch(/Provider worker/);
    expect(screen.queryByText("Keyboard disclosure")).not.toBeInTheDocument();

    const worker = await screen.findByRole("button", { name: /key-worker/ });
    await waitFor(() => expect(metadata.store.getSnapshot().working).toBe(false));
    expect(worker).toHaveAttribute("aria-expanded", "false");
    expect(worker).toHaveAttribute("aria-controls");
    expect(worker.className).toMatch(/focus-visible:outline/);
    expect(worker.getAttribute("aria-label") || "").toMatch(/running/i);
    worker.focus();
    fireEvent.keyDown(worker, { key: " " });
    expect(worker).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Keyboard disclosure")).toBeInTheDocument();
    const details = document.getElementById(worker.getAttribute("aria-controls") || "");
    expect(details).toBeTruthy();
    expect(worker.contains(details)).toBe(false);

    const kill = screen.getByRole("button", { name: "Cancel this job" });
    kill.focus();
    fireEvent.keyDown(kill, { key: " " });
    fireEvent.keyDown(kill, { key: "Enter" });
    expect(job).toHaveAttribute("aria-expanded", "true");
    expect(mockSwarmCancel).not.toHaveBeenCalled();
    fireEvent.click(kill);
    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledWith({ version: 1, source: "local", repo: "/cancel-repo", session_id: "sess-test", job_ref: { job_id: "local-keys", state_id: null }, local_incarnation: metadata.local_ref.incarnation });
      expect(mockSwarmCancel).toHaveBeenCalledTimes(1);
    });
  });
});

describe("SwarmPane pin attribution", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps captured identity scannable without inferring pin policy", async () => {
    const fixture = await capturedRoutingFixture({ artifacts: [routingReference()] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    expect(within(worker.details).getByText('run-task-a', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('Pin attribution unknown')).toBeVisible();
    expect(worker.details).not.toHaveTextContent('user_pin');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Artifacts', exact: true }));
    const ref = screen.getByText("ROUTING / route-ref: unknown");
    fireEvent.click(ref);
    const record = ref.closest('details');
    if (!record) throw Error('Missing artifact disclosure');
    expect(record).toHaveAttribute('open');
    expect(within(record).getByText(referenceHash, { exact: true })).toBeVisible();
    expect(within(record).getByText('task-a', { exact: true })).toBeVisible();
    expect(within(record).getByText('Recorded; contents not independently verified. Artifact body unavailable.')).toBeVisible();
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("does not paint a missing routing estimate as $0", async () => {
    const fixture = await expertMetadataFixture([expertSummary(selection(), "Audit auth flow")]);
    const view = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      fireEvent.click(await screen.findByRole("button", { name: /Audit auth flow/ }));
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
      const worker = await screen.findByText("task-1: running");
    expect(worker).not.toHaveTextContent("—");
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      fixture.dispose();
    }
  });

  it("fail-closes missing policy as Pin attribution unknown (not Router pick)", async () => {
    const fixture = await expertMetadataFixture([expertSummary(selection(), "Audit auth flow")]);
    const view = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      fireEvent.click(await screen.findByRole("button", { name: /Audit auth flow/ }));
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
      const worker = await screen.findByText("task-1: running");
    fireEvent.click(worker);
    expect(screen.getByText("Pin attribution unknown")).toBeVisible();
    expect(screen.queryByText("Router pick")).not.toBeInTheDocument();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      fixture.dispose();
    }
  });

  it("preserves verbatim bounded finding references without fabricating prompt echo content", async () => {
    const fixture = await capturedRoutingFixture({ artifacts: [routingReference('echo-prompt-ref', 'FINDING')] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Artifacts', exact: true }));
    const ref = screen.getByText("FINDING / echo-prompt-ref: unknown");
    fireEvent.click(ref);
    const record = ref.closest('details');
    if (!record) throw Error('Missing artifact disclosure');
    expect(record).toHaveAttribute('open');
    expect(within(record).getByText(referenceHash, { exact: true })).toBeVisible();
    expect(within(record).getByText('task-a', { exact: true })).toBeVisible();
    expect(within(record).getByText('Recorded; contents not independently verified. Artifact body unavailable.')).toBeVisible();
    expect(record).toHaveAttribute('data-artifact-ids', 'echo-prompt-ref');
    expect(fixture.inspector).not.toHaveTextContent('looks like prompt echo');
    expect(fixture.inspector).not.toHaveTextContent('Echo finding warn');
    expect(fixture.inspector).not.toHaveTextContent('confidence:');
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });
});

describe("SwarmPane routing dedupe", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps distinct captured attempts for one task without selecting a final model", async () => {
    const fixture = await capturedRoutingFixture({ attempts: [
      { task: 'task-a', model: 'router-capture', id: 'capture-0' },
      { task: 'task-a', model: 'fallback-capture', id: 'capture-1' },
    ] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText('router-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('fallback-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getAllByText(/Historical identity; current worker model unconfirmed/)).toHaveLength(2);
    expect(within(worker.details).getByText(/Captured attempt capture-0/)).toBeVisible();
    expect(within(worker.details).getByText(/Captured attempt capture-1/)).toBeVisible();
    const other = workerDetails('task-b');
    fireEvent.click(other.button);
    expect(within(other.details).getByText('task-b', { exact: true })).toBeVisible();
    expect(other.details).not.toHaveTextContent('Captured attempt');
    expect(other.details).not.toHaveTextContent('capture-');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-0')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-1')).toBeVisible();
    expect(within(routing).getAllByText('Task: task-a. Run: run-task-a.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps fallback and escalation captures distinct without assigning precedence", async () => {
    const fixture = await capturedRoutingFixture({ attempts: [
      { task: 'task-a', model: 'fallback-capture', id: 'capture-0' },
      { task: 'task-a', model: 'escalation-capture', id: 'capture-1' },
    ] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText('fallback-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('escalation-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getAllByText(/Historical identity; current worker model unconfirmed/)).toHaveLength(2);
    expect(within(worker.details).getByText(/Captured attempt capture-0/)).toBeVisible();
    expect(within(worker.details).getByText(/Captured attempt capture-1/)).toBeVisible();
    const other = workerDetails('task-b');
    fireEvent.click(other.button);
    expect(within(other.details).getByText('task-b', { exact: true })).toBeVisible();
    expect(other.details).not.toHaveTextContent('Captured attempt');
    expect(other.details).not.toHaveTextContent('capture-');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-0')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-1')).toBeVisible();
    expect(within(routing).getAllByText('Task: task-a. Run: run-task-a.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("counts distinct FINDING digests while retaining every artifact reference", async () => {
    const selected = selection();
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selected, 'Grouped findings'), lifecycle: 'complete', artifact_count: 5,
    }]);
    const detail = expertDetail(selected, metadata.context());
    metadata.selected.mockResolvedValue({ ...detail, lifecycle: 'complete', artifact_count: 5,
      artifacts: { page: { ...detail.artifacts.page, scanned: 5 }, rows: Array.from({ length: 5 }, (_, index) => ({
        id: `finding-${index}`, status: null, stamp: 'known', revision: index + 1,
        task_id: 'task-1', type: 'finding', sha256: (index === 0 ? 'a' : 'b').repeat(64),
        presence: 'recorded', check_result: 'unavailable',
      })) },
    });
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      fireEvent.click(await screen.findByRole('button', { name: /^Grouped findings/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
      expect(within(inspector).getByText('Findings (2)')).toBeVisible();
      expect(within(inspector).queryByText(/Findings \(2 of 5\)/)).toBeNull();
      fireEvent.click(within(inspector).getByRole('button', { name: 'Artifacts', exact: true }));
      expect(within(inspector).getByText('5 artifact records shown of 5. Page: complete.')).toBeVisible();
      expect(inspector.querySelectorAll('[data-finding-id]')).toHaveLength(5);
      expect(metadata.selected).toHaveBeenCalledWith(selected, expect.objectContaining({ artifact_cursor: null }));
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });
});

describe("SwarmPane mid-run job-row meters", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("shows native receipt tokens and separately labeled spend savings and forecast", async () => {
    const row = nativeEconomicsRow('local-cost');
    row.economics = { kind: 'estimated', spend_usd: 0.05, estimated: true, cost_provenance: 'static',
      source: 'financial_receipt', estimated_savings_usd: 0.0553, route_forecast_usd: 0.1 };
    const fixture = await economicsNativeFixture([row]);
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      await expandJob(/^Provider worker · local-cost ·/);
      expect(screen.getByText('12,000 combined tokens · reported by native job')).toBeVisible();
      expect(screen.getByText('Estimated spend: $0.05 · static pricing · financial receipt')).toBeVisible();
      expect(screen.getByText('Estimated savings: $0.0553 · financial receipt estimate; not measured savings.')).toBeVisible();
      expect(screen.getByText('Route forecast: $0.1000 · financial receipt estimate; not spend.')).toBeVisible();
      expect(screen.queryByText(/compact|cached|Measured spend:/)).not.toBeInTheDocument();
      expect(screen.getByText(/This view does not calculate totals/)).toBeVisible();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { mounted.unmount(); fixture.dispose(); }
  });

  it("refreshes native receipt savings without changing spend or identity", async () => {
    const row = nativeEconomicsRow('local-update');
    const fixture = await economicsNativeFixture([row]);
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      await expandJob(/^Provider worker · local-update ·/);
      expect(screen.getByText(/Estimated savings: \$0.0200/)).toBeVisible();
      await waitFor(() => expect(fixture.store.getSnapshot().working).toBe(false));
      await fixture.update([{ ...row, revision: 2, economics: { ...row.economics, estimated_savings_usd: 0.11 } }]);
      expect(screen.getByText(/Estimated savings: \$0.1100/)).toBeVisible();
      expect(screen.queryByText(/Estimated savings: \$0.0200/)).not.toBeInTheDocument();
      expect(screen.getByText('Estimated spend: $0.05 · static pricing · financial receipt')).toBeVisible();
      expect(fixture.store.getSnapshot().local.observations.find(o => o.row.local_ref.job_id === row.local_ref.job_id)?.row.revision).toBe(2);
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { mounted.unmount(); fixture.dispose(); }
  });

  it("marks selected artifacts stale and replaces them after lifecycle changes", async () => {
    const selected = expansionSummary('job_outcome_flip', 'Outcome flips', '/repo', 'complete');
    const fixture = await expertMetadataFixture([selected]);
    const detail = expertDetail(selected.selection, fixture.context());
    detail.lifecycle = 'complete';
    fixture.selected.mockResolvedValue(detail);
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      await expandJob(/^Outcome flips ·/);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByRole('region', { name: 'Selected job inspector' });
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      expect(screen.getByText('finding / artifact-1: unknown')).toBeVisible();
      const original = fixture.request.getMockImplementation();
      if (!original) throw Error('Missing wire responder');
      fixture.request.mockImplementation(async (method, path) => {
        const result = await original(method, path);
        if (new URL(path, 'http://fixture').pathname !== '/api/jobs/metadata') return result;
        if (!result || typeof result !== 'object') throw Error('Missing metadata list');
        const url = new URL(path, 'http://fixture');
        const rows = !url.searchParams.get('status') || url.searchParams.get('status') === 'failed'
          ? [{ ...selected, revision: 2, lifecycle: 'failed' }] : [];
        return { ...result, rows, page: { outcome: 'complete', revision: 10000, checkpoint: 10000, scanned: rows.length, next_cursor: null } };
      });
      for (let i = 0; i < 12 && fixture.store.getSnapshot().observations[0].row.lifecycle !== 'failed'; i++) {
        await act(async () => { await fixture.store.advance(); });
      }
      expect(screen.getByText('Retained selected details are stale. Retry inspection to refresh.')).toBeVisible();
      const refreshed = { ...detail, lifecycle: 'failed', artifacts: { ...detail.artifacts, rows: [
        { ...detail.artifacts.rows[0], id: 'verification-2', type: 'verification', revision: 4, status: 'failed' },
      ] } };
      fixture.selected.mockResolvedValue(refreshed);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByText('verification / verification-2: failed');
      expect(screen.queryByText('finding / artifact-1: unknown')).not.toBeInTheDocument();
      expect(screen.queryByText(/Retained selected details are stale/)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /^Outcome flips · failed/ })).toBeVisible();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { mounted.unmount(); fixture.dispose(); }
  });

  it("refuses visibility-only jobs in jobSavings totals", () => {
    const parts = jobSavings({
      id: "j-ext",
      goal: "x",
      status: "running",
      source: "cli",
      accounting_owned: false,
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
      cache_saved_usd: 0.05,
      tool_output_savings_usd: 0.01,
    } as Job);
    expect(parts.total).toBe(0);
  });

  it("treats missing accounting_owned on CLI as visibility-only", () => {
    const parts = jobSavings({
      id: "j-ext",
      goal: "x",
      status: "running",
      source: "cli",
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
    } as Job);
    expect(parts.total).toBe(0);
  });

  it("refuses unknown routing basis in jobSavings totals", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      routing_saved_usd: 1.25,
      routing_savings_basis: "unknown",
      cache_saved_usd: 0.05,
    } as Job);
    expect(parts.routing).toBe(0);
    expect(parts.modelSelection).toBe(0);
    expect(parts.total).toBeCloseTo(0.05, 8);
  });

  it("keeps estimated routing labeled separately from measured cache", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      routing_saved_usd: 0.40,
      routing_savings_basis: "estimated",
      cache_saved_usd: 0.10,
    } as Job);
    expect(parts.routing).toBeCloseTo(0.40, 8);
    expect(parts.modelSelectionEstimated).toBe(true);
    expect(parts.cache).toBeCloseTo(0.10, 8);
    expect(parts.total).toBeCloseTo(0.50, 8);
  });

  it("marks partial cache value when cached tokens are unpriced", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      cache_saved_usd: 0.10,
      swarm_cache_savings_basis: "unknown",
      swarm_cache_unpriced_tokens: 25_000,
    } as Job);

    expect(parts.cache).toBeCloseTo(0.10, 8);
    expect(parts.cachePartial).toBe(true);
    expect(parts.cacheUnpricedTokens).toBe(25_000);
  });

  it("does not replace measured zero delegation with routing estimate", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      delegation_saved_usd: 0,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
    } as Job);
    expect(parts.delegation).toBe(0);
    expect(parts.modelSelection).toBe(0);
    expect(parts.routing).toBeCloseTo(1.25, 8);
    expect(parts.total).toBe(0);
  });

  it("renders local-job routing policy on worker expansion without template prose", async () => {
    // Explicit retained fallback test data on the real native owner/transport contract.
    const route = { task_id: 'local-swarm-1-w0', association: 'legacy_owner_single_task' as const,
      model_kind: 'forecast' as const, role: 'implement', policy: 'balanced', adapter: 'agentic',
      detail: 'balanced pick', est_cost_usd: 0.02, truncated: false };
    const metadata = await nativeExpertFixture({ jobId: 'local-swarm-1', routes: [
      { ...route, ordinal: 0, model: 'initial-cheap', created_by: 'router' },
      { ...route, ordinal: 1, model: 'cheap-model', created_by: 'router-fallback' },
    ] });
    outcomeFixture = metadata;
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    await expandVisibleJobs();
    const worker = await screen.findByRole("button", { name: /implement \(agentic\)/ });
    await waitFor(() => expect(worker).toHaveTextContent("cheap-model"));
    expect(worker).toHaveTextContent("recorded route forecast");
    expect(screen.queryByTitle("Model: cheap-model")).toBeNull();
    expect(screen.queryByText("initial-cheap")).not.toBeInTheDocument();
    expect(screen.queryByText("balanced")).not.toBeInTheDocument();
    expect(screen.queryByText(/Right-sized: cheapest model/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Unmatched routing/)).not.toBeInTheDocument();

    fireEvent.click(worker);
    expect(screen.getByText("balanced")).toBeInTheDocument();
    expect(screen.getByText("fallback")).toBeInTheDocument();
    expect(screen.queryByText(/Right-sized: cheapest model/)).not.toBeInTheDocument();
    expect(screen.queryByText("Pin attribution unknown")).not.toBeInTheDocument();
    expect(screen.queryByText("Router pick")).not.toBeInTheDocument();
    expect(screen.queryByText(/Unmatched routing/)).not.toBeInTheDocument();
    expect(screen.queryByText(/no matching worker/)).not.toBeInTheDocument();
  });
});

let outcomeFixture: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;

afterEach(() => { outcomeFixture?.dispose(); outcomeFixture = undefined; });

describe("SwarmPane truthful failed vs cancelled chrome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("paints ordinary worker failure as failed, not cancelled", async () => {
    outcomeFixture = await expertMetadataFixture([outcomeSummary('job-fail', 'Ordinary failure', 'failed')]);
    render(<outcomeFixture.Provider><SwarmPane /></outcomeFixture.Provider>);
    expect(screen.getByRole('button', { name: /Finished/ })).toBeVisible();
    expect(screen.getByText('1 failed')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Ordinary failure · failed' })).toBeVisible();
    expect(screen.getByText('failed')).toHaveClass('text-risk');
    expect(screen.queryByText('cancelled')).not.toBeInTheDocument();
    expect(mockSwarmLive).not.toHaveBeenCalled();
  });

  it("moves timed-out jobs into Finished with failed worker chrome", async () => {
    const row = outcomeSummary('job-timeout', 'Timed-out command', 'timeout');
    outcomeFixture = await expertMetadataFixture([row]);
    const f = outcomeFixture;
    f.selected.mockImplementation(async selected => {
      const result = expertDetail(selected, f.context());
      return { ...result, lifecycle: 'timeout', tasks: { ...result.tasks, rows: [{ ...result.tasks.rows[0], status: 'timeout' }] } };
    });
    render(<f.Provider><SwarmPane /></f.Provider>);
    expect(screen.getByRole('button', { name: /Finished/ })).toBeVisible();
    expect(screen.getByText('1 failed')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Timed-out command · failed' }));
    fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
    expect(await screen.findByLabelText('Workers')).toHaveTextContent('1/1');
    expect(screen.getByText('task-1: timeout')).toBeVisible();
    expect(screen.getByText('task-1: timeout')).toHaveClass('text-risk');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Dismiss from tracker: Timed-out command' })).toBeVisible();

    f.selected.mockImplementation(async selected => {
      const result = expertDetail(selected, f.context());
      const statuses = ['timed_out', 'timeout', 'truncated', 'interrupted', 'partial', 'stalled', 'cancelled', 'running'];
      return { ...result, lifecycle: 'timeout', tasks: { ...result.tasks,
        page: { ...result.tasks.page, scanned: statuses.length },
        rows: statuses.map((status, index) => ({ ...result.tasks.rows[0], id: `outcome-${index}`, binding: null, status })) } };
    });
    fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
    await waitFor(() => expect(screen.getByLabelText('Workers')).toHaveTextContent(
      '7/8 observed workers finished · 0 completed · 4 failed · 1 cancelled · 1 partial · 1 recoverable',
    ));
    expect(screen.getByText('outcome-3: interrupted')).toHaveClass('text-risk');
    expect(screen.getByText('outcome-6: cancelled')).not.toHaveClass('text-risk');
  });

  it("moves truncated and interrupted jobs into Finished with failed chrome", async () => {
    outcomeFixture = await expertMetadataFixture([
      outcomeSummary('job-truncated', 'Truncated command', 'truncated'),
      outcomeSummary('job-interrupted', 'Interrupted command', 'interrupted'),
    ]);
    render(<outcomeFixture.Provider><SwarmPane /></outcomeFixture.Provider>);
    expect(screen.getByRole('button', { name: 'Finished (2 observed)' })).toBeVisible();
    expect(screen.getByText('2 failed')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Truncated command · failed' })).toBeVisible();
    const interrupted = screen.getByRole('button', { name: 'Interrupted command · interrupted' });
    expect(interrupted).toBeVisible();
    expect(within(interrupted).getByText('interrupted')).toHaveClass('text-risk');
  });

  it("keeps completed evidence unverified and separates failed from cancelled lifecycle", async () => {
    const fixture = await qualityFixture({ lifecycle: 'complete', tasks: [identityTask('task-a', 'complete')],
      outcomes: [{ task: 'task-a', code: 1, timedOut: false }] }, 'task-a');
    try {
      expect(within(fixture.job).getByText('complete')).toHaveClass('text-muted');
      expect(fixture.job.querySelector('.text-good')).toBeNull();
      const worker = workerDetails('task-a', 'complete');
      fireEvent.click(worker.button);
      expect(within(worker.details).getByText(/Captured process outcome outcome-0/)).toBeVisible();
      expect(within(worker.details).getByText('1')).toBeVisible();
      expect(screen.getByLabelText('Workers')).toHaveTextContent('1/1 observed workers finished · 1 completed · 0 failed');
      fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-record: recorded');
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('completed lifecycle do not establish that checks passed');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('verification / verification-record: complete'));
      expect(screen.getByText(evidenceHash)).toBeVisible();
      expect(screen.getByText('task-a')).toBeVisible();
      rejectQualityVerdicts(fixture.detail);
      const selected = fixture.detail.selection;
      await fixture.replace([
        { ...expertSummary(selected, 'Identity evidence'), lifecycle: 'complete' },
        { ...expertSummary({ ...selected, job_ref: { ...selected.job_ref, job_id: 'job_failed' } }, 'Failed lifecycle'), lifecycle: 'failed' },
        { ...expertSummary({ ...selected, job_ref: { ...selected.job_ref, job_id: 'job_cancelled' } }, 'Cancelled lifecycle'), lifecycle: 'cancelled' },
      ]);
      expect(screen.getByText('1 failed · 1 cancelled')).toBeVisible();
      const failed = screen.getByRole('button', { name: 'Failed lifecycle · failed' });
      const cancelled = screen.getByRole('button', { name: 'Cancelled lifecycle · cancelled' });
      expect(within(failed).getByText('failed')).toHaveClass('text-risk');
      expect(within(cancelled).getByText('cancelled')).toHaveClass('text-muted');
      expect(cancelled.querySelector('.text-risk')).toBeNull();
    } finally { fixture.close(); }
  });

  it("paints true user cancel as cancelled and does not tally it as failed", async () => {
    outcomeFixture = await expertMetadataFixture([outcomeSummary('job-cancel', 'User aborted swarm', 'cancelled')]);
    render(<outcomeFixture.Provider><SwarmPane /></outcomeFixture.Provider>);
    expect(screen.getByRole('button', { name: /Finished/ })).toBeVisible();
    expect(screen.getByText('1 cancelled')).toBeVisible();
    expect(screen.getByRole('button', { name: 'User aborted swarm · cancelled' })).toBeVisible();
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument();
  });
});

describe("SwarmPane cancel Kill contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockSwarmCancel.mockReset();
  });

  it("accepted Kill shows optimistic cancelling then refreshes live", async () => {
    let accept: (value: { ok: boolean }) => void = () => {};
    mockSwarmCancel.mockReturnValue(new Promise(resolve => { accept = resolve; }));
    const f = await nativeControlFixture();
    outcomeFixture = f;
    render(<f.Provider><SwarmPane /></f.Provider>);
    fireEvent.click(screen.getByRole('button', { name: /parallel wave · running/ }));
    const kill = screen.getByRole('button', { name: 'Request native stop' });
    expect(kill).toBeEnabled();
    fireEvent.click(kill);
    expect(kill).toBeDisabled();
    expect(screen.getByText('Awaiting stop acknowledgement.')).toBeVisible();
    expect(mockSwarmCancel).toHaveBeenCalledWith({ version: 1, source: 'local', repo: f.context().repo,
      session_id: f.context().session_id, local_incarnation: 'native-1', job_ref: { job_id: 'local-kill', state_id: null } });
    await act(async () => { f.cancel(); accept({ ok: true }); });
    expect(await screen.findByText('Stop request accepted; observed lifecycle: cancelled.')).toBeVisible();
    expect(f.request.mock.calls.some(([, path]) => path.includes('/metadata/local/detail?'))).toBe(true);
    expect(screen.queryByText(/force/i)).not.toBeInTheDocument();
    expect(mockSwarmLive).not.toHaveBeenCalled();
  });

  it("rejected Kill clears cancelling and leaves Kill retryable", async () => {
    mockSwarmCancel.mockResolvedValue({ ok: false, error: 'not running' });
    const f = await nativeControlFixture();
    outcomeFixture = f;
    render(<f.Provider><SwarmPane /></f.Provider>);
    fireEvent.click(screen.getByRole('button', { name: /parallel wave · running/ }));
    const kill = screen.getByRole('button', { name: 'Request native stop' });
    expect(kill).toBeEnabled();
    fireEvent.click(kill);
    expect(await screen.findByText('not running')).toBeVisible();
    expect(mockSwarmCancel).toHaveBeenCalledWith({ version: 1, source: 'local', repo: f.context().repo,
      session_id: f.context().session_id, local_incarnation: 'native-1', job_ref: { job_id: 'local-kill', state_id: null } });
    expect(kill).toBeEnabled();
    expect(screen.queryByText('Awaiting stop acknowledgement.')).not.toBeInTheDocument();
    fireEvent.click(kill);
    await waitFor(() => expect(mockSwarmCancel).toHaveBeenCalledTimes(2));
  });

  it("stale/404 Kill stays unconfirmed until explicit inspection before retry", async () => {
    mockSwarmCancel.mockRejectedValueOnce(Object.assign(new Error('Not Found'), { status: 404 }))
      .mockResolvedValueOnce({ ok: true });
    const f = await nativeControlFixture();
    outcomeFixture = f;
    render(<f.Provider><SwarmPane /></f.Provider>);
    fireEvent.click(screen.getByRole('button', { name: /parallel wave · running/ }));
    const kill = screen.getByRole('button', { name: 'Request native stop' });
    expect(kill).toBeEnabled();
    fireEvent.click(kill);
    await waitFor(() => expect(kill).toBeEnabled());
    expect(screen.queryByText('Awaiting stop acknowledgement.')).not.toBeInTheDocument();
    expect(screen.getByText('Stop outcome is unconfirmed. Inspect the job before retrying.')).toBeVisible();
    expect(f.request.mock.calls.filter(([, path]) => path.includes('/metadata/local/detail?'))).toHaveLength(0);
    const selection = { version: 1, source: 'local', repo: f.context().repo,
      session_id: f.context().session_id, local_incarnation: 'native-1', job_ref: { job_id: 'local-kill', state_id: null } };
    expect(mockSwarmCancel.mock.calls).toEqual([[selection]]);
    fireEvent.click(screen.getByRole('button', { name: 'Inspect actions' }));
    await screen.findByText(/actions: complete/);
    const reads = f.request.mock.calls.filter(([, path]) => path.includes('/metadata/local/detail?'));
    expect(reads).toHaveLength(1);
    expect(reads[0][0]).toBe('GET');
    const url = new URL(reads[0][1], 'http://fixture');
    expect(url.pathname).toBe('/api/jobs/metadata/local/detail');
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ job_id: 'local-kill', incarnation: 'native-1',
      session_id: f.context().session_id, repo: f.context().repo, view_generation: f.context().view_generation, lane: 'actions' });
    expect(url.searchParams.has('cursor')).toBe(false);
    expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
    expect(screen.getByText('Stop outcome is unconfirmed. Inspect the job before retrying.')).toBeVisible();
    expect(mockSwarmCancel.mock.calls).toEqual([[selection]]);
    expect(kill).toBeEnabled();
    fireEvent.click(kill);
    await waitFor(() => expect(mockSwarmCancel).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Stop request accepted; awaiting lifecycle observation. Retry inspection to check again.')).toBeVisible();
    expect(mockSwarmCancel.mock.calls).toEqual([[selection], [selection]]);
    expect(f.request.mock.calls.filter(([, path]) => path.includes('/metadata/local/detail?'))).toEqual([reads[0], reads[0]]);
    expect(mockSwarmLive).not.toHaveBeenCalled();
  });
});

describe("SwarmPane canonical outcome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps completed lifecycle neutral with a positively recorded finding", async () => {
    const fixture = await evidenceFixture('Completed audit');
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      const row = await screen.findByRole('button', { name: 'Completed audit · complete' });
      expect(screen.getByRole('button', { name: 'Finished (1 observed)' })).toBeVisible();
      expect(within(row).getByText('complete')).toBeVisible();
      expect(within(row).getByText('complete')).toHaveClass('text-muted');
      expect(row.querySelector('.text-good')).toBeNull();
      fireEvent.click(row);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      expect(await screen.findByText('Findings (1)')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      expect(screen.getByText('finding / harness-finding: unknown')).toBeVisible();
      expect(screen.queryByText(/only verification artifacts/)).not.toBeInTheDocument();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { cleanup(); fixture.dispose(); }
  });
});

describe("SwarmPane findings section collapse", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("collapses and reopens the recorded finding hash in Artifacts without refetching", async () => {
    const fixture = await evidenceFixture('Audit findings collapse');
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      fireEvent.click(await screen.findByRole('button', { name: 'Audit findings collapse · complete' }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByText('Findings (1)');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      const artifact = screen.getByText('finding / harness-finding: unknown');
      const disclosure = artifact.closest('details');
      expect(disclosure).not.toHaveAttribute('open');
      expect(screen.getByText(evidenceHash)).not.toBeVisible();
      fireEvent.click(artifact);
      expect(disclosure).toHaveAttribute('open');
      expect(screen.getByText(evidenceHash)).toBeVisible();
      fireEvent.click(artifact);
      expect(screen.getByText(evidenceHash)).not.toBeVisible();
      fireEvent.click(artifact);
      expect(screen.getByText(evidenceHash)).toBeVisible();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
      expect(fetchJobArtifacts).not.toHaveBeenCalled();
    } finally { cleanup(); fixture.dispose(); }
  });

  it("loads selected artifact metadata only after explicit inspection of a finished job", async () => {
    const fixture = await evidenceFixture('Slim finished swarm');
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      expect(fixture.selected).not.toHaveBeenCalled();
      fireEvent.click(await screen.findByRole('button', { name: 'Slim finished swarm · complete' }));
      expect(fixture.selected).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByText('Findings (1)');
      expect(fixture.selected).toHaveBeenCalledWith(evidenceSelection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('finding / harness-finding: unknown'));
      expect(screen.getByText(evidenceHash)).toBeVisible();
      expect(mockArtifacts).not.toHaveBeenCalled();
      expect(fetchJobArtifacts).not.toHaveBeenCalled();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { cleanup(); fixture.dispose(); }
  });

  it("preserves the owned artifact hash when cross-project inspection is disabled", async () => {
    const fixture = await evidenceFixture('Owned sibling slim');
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      const row = await screen.findByRole('button', { name: 'Owned sibling slim · complete' });
      fireEvent.click(row);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByText('Findings (1)');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('finding / harness-finding: unknown'));
      expect(screen.getByText(evidenceHash)).toBeVisible();
      const boundary = render(<fixture.Provider><MetadataInspection job={{ id: evidenceSelection.job_ref.job_id,
        job_ref: evidenceSelection.job_ref, source: 'harness', session_id: evidenceSelection.session_id,
        status: 'complete', cross_project: true, metadata_key: metadataSelectionKey(evidenceSelection) }} /></fixture.Provider>);
      expect(within(boundary.container).getByRole('button', { name: 'Inspect tasks and artifacts' })).toBeDisabled();
      expect(screen.getByText(evidenceHash)).toBeVisible();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
      expect(mockArtifacts).not.toHaveBeenCalled();
      expect(fetchJobArtifacts).not.toHaveBeenCalled();
    } finally { cleanup(); fixture.dispose(); }
  });
});

describe("SwarmPane worker-owned routing surface", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps long task identity separate from collapsed historical disclosures", async () => {
    const fixture = await capturedRoutingFixture({ tasks: ['task-test-coverage-reviewer-with-a-long-stable-task-identifier', 'task-b'] });
    const worker = workerDetails("task-test-coverage-reviewer-with-a-long-stable-task-identifier", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    expect(worker.button).toHaveTextContent('task-test-coverage-reviewer-with-a-long-stable-task-identifier: running');
    expect(worker.button).not.toHaveTextContent('captured-model');
    expect(within(worker.details).getByText('role-task-test-coverage-reviewer-with-a-long-stable-task-identifier', { exact: true })).toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).not.toBeVisible();
    expect(screen.queryByText('secret worker instructions')).not.toBeInTheDocument();
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps unmatched captured attempts in Routing without inheriting them to workers", async () => {
    const fixture = await capturedRoutingFixture({ tasks: ['task-a', 'task-b'], attempts: [
      { task: 'unmatched-task', model: 'unmatched-capture', id: 'unmatched-1' },
      { task: 'unmatched-task', model: 'later-capture', id: 'unmatched-2' },
    ] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText('role-task-a', { exact: true })).toBeVisible();
    expect(worker.details).not.toHaveTextContent('unmatched-capture');
    expect(worker.details).not.toHaveTextContent('later-capture');
    expect(worker.details).not.toHaveTextContent('Captured attempt');
    expect(screen.getByRole('button', { name: /^Captured routing evidence/ })).not.toHaveTextContent('unmatched-capture');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-1')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-2')).toBeVisible();
    expect(within(routing).getAllByText('Task: unmatched-task. Run: run-unmatched-task.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps job captures historical without promoting a header model", async () => {
    const fixture = await capturedRoutingFixture({ tasks: ['task-a', 'task-b'], attempts: [
      { task: 'unmatched-task', model: 'unmatched-capture', id: 'unmatched-1' },
      { task: 'unmatched-task', model: 'later-capture', id: 'unmatched-2' },
    ] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText('role-task-a', { exact: true })).toBeVisible();
    expect(worker.details).not.toHaveTextContent('unmatched-capture');
    expect(worker.details).not.toHaveTextContent('later-capture');
    expect(worker.details).not.toHaveTextContent('Captured attempt');
    expect(screen.getByRole('button', { name: /^Captured routing evidence/ })).not.toHaveTextContent('unmatched-capture');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-1')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-2')).toBeVisible();
    expect(within(routing).getAllByText('Task: unmatched-task. Run: run-unmatched-task.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("discloses captured identity and route references without rejected alternative inference", async () => {
    const fixture = await capturedRoutingFixture({ artifacts: [routingReference('decision-ref')] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(within(worker.details).getByText('captured-model', { exact: true })).toBeVisible();
    expect(worker.details).not.toHaveTextContent('Rejected alternatives');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Artifacts', exact: true }));
    const ref = screen.getByText("ROUTING / decision-ref: unknown");
    fireEvent.click(ref);
    const record = ref.closest('details');
    if (!record) throw Error('Missing artifact disclosure');
    expect(record).toHaveAttribute('open');
    expect(within(record).getByText(referenceHash, { exact: true })).toBeVisible();
    expect(within(record).getByText('task-a', { exact: true })).toBeVisible();
    expect(within(record).getByText('Recorded; contents not independently verified. Artifact body unavailable.')).toBeVisible();
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(routing).not.toHaveTextContent('Rejected alternatives');
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps selected receipt estimated plan zero distinct from unknown API cost", async () => {
    const selected: MetadataSelection = { ...selection(), job_ref: { ...selection().job_ref,
      version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selected, 'Selected worker cost'), lifecycle: 'complete',
    }]);
    const unknown: SelectedMetric = { total: null, state: 'unknown', known_selected: 0,
      unknown_selected: 1, estimated_selected: 0, conflicting_selected: 0 };
    const planZero: SelectedMetric = { total: 0, state: 'estimated', known_selected: 1,
      unknown_selected: 0, estimated_selected: 1, conflicting_selected: 0 };
    metadata.selected.mockResolvedValue({
      ...expertDetail(selected, metadata.context()), lifecycle: 'complete',
      cost: { kind: 'available', outcome: 'available', job_ref: selected.job_ref,
        source: 'terminal_receipt', coverage: 'selected_receipt', summary_revision: 8,
        receipt_digest: 'a'.repeat(64), selected_count: 1, reason: null, retry_after_ms: null,
        totals: { tokens_in: unknown, tokens_out: unknown, cache_read_tokens: unknown,
          cache_write_tokens: unknown, api_cost_usd: unknown,
          plan_marginal_cost_usd: planZero, api_equivalent_cost_usd: unknown } },
    });
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      fireEvent.click(await screen.findByRole('button', { name: /^Selected worker cost/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
      fireEvent.click(within(inspector).getByRole('button', { name: 'Economics', exact: true }));
      expect(within(inspector).getByText('Selected API cost: unknown.')).toBeVisible();
      expect(within(inspector).getByText('Selected plan marginal cost: $0 (estimated).')).toBeVisible();
      expect(within(inspector).getByText('Source: frozen terminal receipt. Selected records: 1.')).toBeVisible();
      expect(within(inspector).getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeVisible();
      expect(metadata.selected).toHaveBeenCalledWith(selected, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });

  it("keeps selected receipt measured API zero distinct from estimated plan zero", async () => {
    const selected: MetadataSelection = { ...selection(), job_ref: { ...selection().job_ref,
      version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selected, 'Selected worker cost'), lifecycle: 'complete',
    }]);
    const unknown: SelectedMetric = { total: null, state: 'unknown', known_selected: 0,
      unknown_selected: 1, estimated_selected: 0, conflicting_selected: 0 };
    const measuredZero: SelectedMetric = { total: 0, state: 'measured', known_selected: 1,
      unknown_selected: 0, estimated_selected: 0, conflicting_selected: 0 };
    const planZero: SelectedMetric = { total: 0, state: 'estimated', known_selected: 1,
      unknown_selected: 0, estimated_selected: 1, conflicting_selected: 0 };
    metadata.selected.mockResolvedValue({
      ...expertDetail(selected, metadata.context()), lifecycle: 'complete',
      cost: { kind: 'available', outcome: 'available', job_ref: selected.job_ref,
        source: 'terminal_receipt', coverage: 'selected_receipt', summary_revision: 8,
        receipt_digest: 'a'.repeat(64), selected_count: 1, reason: null, retry_after_ms: null,
        totals: { tokens_in: unknown, tokens_out: unknown, cache_read_tokens: unknown,
          cache_write_tokens: unknown, api_cost_usd: measuredZero,
          plan_marginal_cost_usd: planZero, api_equivalent_cost_usd: unknown } },
    });
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      fireEvent.click(await screen.findByRole('button', { name: /^Selected worker cost/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
      fireEvent.click(within(inspector).getByRole('button', { name: 'Economics', exact: true }));
      expect(within(inspector).getByText('Selected API cost: $0 (measured).')).toBeVisible();
      expect(within(inspector).getByText('Selected plan marginal cost: $0 (estimated).')).toBeVisible();
      expect(within(inspector).getByText('Source: frozen terminal receipt. Selected records: 1.')).toBeVisible();
      expect(within(inspector).getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeVisible();
      expect(metadata.selected).toHaveBeenCalledWith(selected, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });

  it("keeps selected receipt measured API zero with unknown plan marginal cost", async () => {
    const selected: MetadataSelection = { ...selection(), job_ref: { ...selection().job_ref,
      version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selected, 'Selected worker cost'), lifecycle: 'complete',
    }]);
    const unknown: SelectedMetric = { total: null, state: 'unknown', known_selected: 0,
      unknown_selected: 1, estimated_selected: 0, conflicting_selected: 0 };
    const measuredZero: SelectedMetric = { total: 0, state: 'measured', known_selected: 1,
      unknown_selected: 0, estimated_selected: 0, conflicting_selected: 0 };
    metadata.selected.mockResolvedValue({
      ...expertDetail(selected, metadata.context()), lifecycle: 'complete',
      cost: { kind: 'available', outcome: 'available', job_ref: selected.job_ref,
        source: 'terminal_receipt', coverage: 'selected_receipt', summary_revision: 8,
        receipt_digest: 'a'.repeat(64), selected_count: 1, reason: null, retry_after_ms: null,
        totals: { tokens_in: unknown, tokens_out: unknown, cache_read_tokens: unknown,
          cache_write_tokens: unknown, api_cost_usd: measuredZero,
          plan_marginal_cost_usd: unknown, api_equivalent_cost_usd: unknown } },
    });
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      fireEvent.click(await screen.findByRole('button', { name: /^Selected worker cost/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
      fireEvent.click(within(inspector).getByRole('button', { name: 'Economics', exact: true }));
      expect(within(inspector).getByText('Selected API cost: $0 (measured).')).toBeVisible();
      expect(within(inspector).getByText('Selected plan marginal cost: unknown.')).toBeVisible();
      expect(within(inspector).getByText('Source: frozen terminal receipt. Selected records: 1.')).toBeVisible();
      expect(within(inspector).getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeVisible();
      expect(metadata.selected).toHaveBeenCalledWith(selected, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });
});

describe("SwarmPane worker tokens and cost", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("discloses separate captured task token and estimated cost records in History", async () => {
    const selected = { ...evidenceSelection, job_ref: { ...evidenceSelection.job_ref, version: 2,
      incarnation: '629c4989-c0c5-4211-92ef-bca81be91ca8' } } satisfies MetadataSelection;
    const fixture = await evidenceFixture('Multi worker spend', [selected]);
    const detail = capturedModelDetail(selected, fixture.context());
    const captured = detail.history.process_outcomes.rows[0];
    detail.history.process_outcomes.rows = [
      { ...captured, sequence: 3, facts: { ...captured.facts, task_id: 't1', attempt_id: 'attempt-1', observation_id: 'observation-1', run_id: 'run-1',
        tokens_in: 120000, cost_usd: 0.14, cost_state: 'estimated', cost_basis: 'api' } },
      { ...captured, sequence: 4, facts: { ...captured.facts, task_id: 't2', attempt_id: 'attempt-2', observation_id: 'observation-2', run_id: 'run-2',
        tokens_in: 60000, cost_usd: 0.07, cost_state: 'estimated', cost_basis: 'api' } },
    ];
    detail.history.process_outcomes.page.scanned = 2;
    detail.history.process_outcomes.page.captured_count = 2;
    detail.history.counts.captured_process_outcomes = 2;
    fixture.selected.mockResolvedValue(detail);
    try {
      render(<fixture.Provider><SwarmPane /></fixture.Provider>);
      fireEvent.click(await screen.findByRole('button', { name: 'Multi worker spend · complete' }));
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByRole('region', { name: 'Selected job inspector' });
      fireEvent.click(screen.getByRole('button', { name: 'History', exact: true }));
      const outcomes = screen.getByRole('region', { name: 'process outcomes' });
      const first = within(outcomes).getByText('outcome 3').closest('details');
      const second = within(outcomes).getByText('outcome 4').closest('details');
      if (!first || !second) throw Error('Captured worker outcomes missing');
      expect(within(first).getByText('120000')).not.toBeVisible();
      expect(within(second).getByText('60000')).not.toBeVisible();
      fireEvent.click(within(first).getByText('outcome 3'));
      expect(within(first).getByText('t1')).toBeVisible();
      expect(within(first).getByText('120000')).toBeVisible();
      expect(within(first).getByText('0.14')).toBeVisible();
      expect(within(first).getByText('estimated')).toBeVisible();
      expect(within(first).getByText('api')).toBeVisible();
      expect(within(second).getByText('0.07')).not.toBeVisible();
      fireEvent.click(within(second).getByText('outcome 4'));
      expect(within(second).getByText('t2')).toBeVisible();
      expect(within(second).getByText('60000')).toBeVisible();
      expect(within(second).getByText('0.07')).toBeVisible();
      expect(within(second).getByText('estimated')).toBeVisible();
      expect(within(first).queryByText('0.07')).not.toBeInTheDocument();
      fireEvent.click(within(first).getByText('outcome 3'));
      expect(within(first).getByText('0.14')).not.toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Economics', exact: true }));
      expect(screen.getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeVisible();
      expect(screen.queryByText(/0.21/)).not.toBeInTheDocument();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { cleanup(); fixture.dispose(); }
  });
});

describe("SwarmPane worker progress", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("labels failed workers separately from completed ones", async () => {
    await renderWorkerMetadata('Mixed worker outcomes', 'running', ['completed', 'failed', 'running']);
    expect(screen.getByText("Workers (3)")).toBeInTheDocument();
    expect(screen.getByLabelText("Workers")).toHaveTextContent("2/3");
    expect(screen.getByText("t2: failed")).toBeInTheDocument();
    expect(screen.getByLabelText("Workers")).toHaveTextContent("1 completed · 1 failed");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});

describe("SwarmPane worker outcome hierarchy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("maps lifecycle and exact-task artifacts in workerOutcome", () => {
    const complete = { id: "t", status: "complete", role: "w", instruction: "", adapter: "agentic" };
    const idle = { id: "t", status: "queued", role: "w", instruction: "", adapter: "agentic" };
    const failedArt = { type: "verification", headline: "", result: "failed" as const };
    expect(workerOutcome(complete, failedArt)).toBe("degraded");
    expect(workerOutcome(complete, { type: "verification", headline: "", result: "degraded" })).toBe("degraded");
    expect(workerOutcome(complete, { type: "verification", headline: "", result: "blocked" })).toBe("degraded");
    expect(workerOutcome(complete, { type: "finding", headline: "ok" })).toBe("ok");
    expect(workerOutcome(complete)).toBe("ok");
    expect(workerOutcome({ ...complete, status: "failed" })).toBe("failed");
    expect(workerOutcome({ ...complete, status: "running" }, failedArt)).toBe("running");
    expect(workerOutcome(idle, failedArt)).toBe("degraded");
    expect(workerOutcome(idle)).toBe("idle");
  });

  it("discloses exact-task verification and nonzero process evidence without certifying completion", async () => {
    const fixture = await qualityFixture({ lifecycle: 'complete',
      tasks: [identityTask('task-a', 'complete'), identityTask('task-b', 'complete')],
      models: ['verify-model', 'review-model'], outcomes: [{ task: 'task-a', code: 7, timedOut: false }] }, 'task-a');
    try {
      const a = workerDetails('task-a', 'complete');
      const b = workerDetails('task-b', 'complete');
      fireEvent.click(a.button); fireEvent.click(b.button);
      expect(within(a.details).getByText(/Captured process outcome outcome-0/)).toBeVisible();
      expect(within(a.details).getByText('7')).toBeVisible();
      expect(within(a.details).getByText('false')).toBeVisible();
      expect(within(a.details).getByText('verify-model')).toBeVisible();
      expect(within(b.details).getByText('review-model')).toBeVisible();
      expect(within(b.details).queryByText(/Captured process outcome/)).not.toBeInTheDocument();
      expect(within(b.details).queryByText('7')).not.toBeInTheDocument();
      for (const worker of [a, b]) {
        expect(within(worker.button).getByText(/complete/)).toHaveClass('text-muted');
        expect(worker.button.querySelector('.text-good, .text-risk')).toBeNull();
      }
      expect(screen.getByLabelText('Workers')).toHaveTextContent('2/2 observed workers finished · 2 completed · 0 failed');
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-record: recorded');
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('do not establish that checks passed');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('verification / verification-record: complete'));
      const artifact = screen.getByText(evidenceHash).closest('details');
      if (!artifact) throw Error('Missing verification disclosure');
      expect(within(artifact).getByText('task-a')).toBeVisible();
      expect(within(artifact).queryByText('task-b')).not.toBeInTheDocument();
      expect(within(artifact).getByText(evidenceHash)).toBeVisible();
      expect(artifact.querySelector('.text-good')).toBeNull();
      rejectQualityVerdicts(fixture.detail);
    } finally { fixture.close(); }
  });

  it("suppresses x/N text when every worker is ok", async () => {
    await renderWorkerMetadata('Fully successful terminal', 'complete', ['complete', 'complete', 'complete']);
    expect(screen.getByText("Workers (3)")).toBeInTheDocument();
    expect(screen.getByLabelText("Workers")).toHaveTextContent("3/3");
    expect(screen.getByLabelText("Workers")).toHaveTextContent("3 completed · 0 failed");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("keeps x/N and failed count on mixed 2/3 terminal progress", async () => {
    await renderWorkerMetadata('Mixed terminal outcomes', 'running', ['complete', 'failed', 'running']);
    expect(screen.getByLabelText("Workers")).toHaveTextContent("2/3");
    expect(screen.getByText("t2: failed")).toBeInTheDocument();
    expect(screen.getByLabelText("Workers")).toHaveTextContent("1 failed");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("keeps x/N during active 1/3 progress", async () => {
    await renderWorkerMetadata('Active worker progress', 'running', ['complete', 'running', 'running']);
    expect(screen.getByLabelText("Workers")).toHaveTextContent("1/3");
    expect(screen.getByText("Workers (3)")).toBeInTheDocument();
    expect(screen.getByText("t2: running")).toBeInTheDocument();
    expect(screen.getByText("t3: running")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("keeps unlinked verification and process records off both completed workers", async () => {
    const fixture = await qualityFixture({ lifecycle: 'complete',
      tasks: [identityTask('task-a', 'complete'), identityTask('task-b', 'complete')],
      models: ['model-a', 'model-b'], outcomes: [{ task: null, code: 9, timedOut: true }] }, null);
    try {
      for (const [id, model] of [['task-a', 'model-a'], ['task-b', 'model-b']]) {
        const worker = workerDetails(id, 'complete'); fireEvent.click(worker.button);
        expect(within(worker.details).getByText(id)).toBeVisible();
        expect(within(worker.details).getByText(model)).toBeVisible();
        expect(within(worker.details).getByText(`role-${id}`)).toBeVisible();
        expect(within(worker.details).queryByText(/Captured process outcome/)).not.toBeInTheDocument();
        expect(within(worker.details).queryByText('9')).not.toBeInTheDocument();
        expect(worker.button.querySelector('.text-risk, .text-good')).toBeNull();
      }
      expect(screen.getByLabelText('Workers')).toHaveTextContent('2/2 observed workers finished · 2 completed · 0 failed');
      fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-record: recorded');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('verification / verification-record: complete'));
      expect(screen.getByText('link missing')).toBeVisible();
      expect(screen.getByText(evidenceHash)).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'History', exact: true }));
      const outcomes = screen.getByRole('region', { name: 'process outcomes' });
      fireEvent.click(within(outcomes).getByText('outcome 1'));
      expect(within(outcomes).getByText('outcome-0')).toBeVisible();
      expect(within(outcomes).getByText('9')).toBeVisible();
      expect(within(outcomes).getByText('true')).toBeVisible();
      expect(within(outcomes).getByText('unavailable')).toBeVisible();
      expect(within(outcomes).queryByText('task-a')).not.toBeInTheDocument();
      expect(within(outcomes).queryByText('task-b')).not.toBeInTheDocument();
      expect(fixture.job.querySelector('.text-good')).toBeNull();
      rejectQualityVerdicts(fixture.detail);
    } finally { fixture.close(); }
  });

  it("keeps job-level recorded verification separate from exact-task process evidence", async () => {
    const fixture = await qualityFixture({ lifecycle: 'complete',
      tasks: [identityTask('task-a', 'complete'), identityTask('task-b', 'complete')], models: ['model-a', 'model-b'],
      outcomes: [{ task: 'task-b', code: 4, timedOut: true }] }, null);
    try {
      const a = workerDetails('task-a', 'complete'); const b = workerDetails('task-b', 'complete');
      fireEvent.click(a.button); fireEvent.click(b.button);
      expect(within(a.details).getByText('model-a')).toBeVisible();
      expect(within(a.details).queryByText(/Captured process outcome/)).not.toBeInTheDocument();
      expect(within(b.details).getByText(/Captured process outcome outcome-0/)).toBeVisible();
      expect(within(b.details).getByText('4')).toBeVisible();
      expect(within(b.details).getByText('true')).toBeVisible();
      expect(within(b.details).getByText(/Historical process evidence; not a failure diagnosis/)).toBeVisible();
      for (const worker of [a, b]) expect(worker.button.querySelector('.text-good, .text-risk')).toBeNull();
      expect(screen.getByLabelText('Workers')).toHaveTextContent('2 completed · 0 failed');
      expect(screen.getByText('Delivery: unverified. Quality: unverified. Publication and lifecycle do not certify verification.')).toBeVisible();
      expect(fixture.job.querySelector('.text-good, .text-risk')).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-record: recorded');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('verification / verification-record: complete'));
      expect(screen.getByText(evidenceHash)).toBeVisible();
      expect(screen.getByText('link missing')).toBeVisible();
      expect(screen.getByRole('region', { name: 'Artifacts' })).not.toHaveTextContent('task-b');
      rejectQualityVerdicts(fixture.detail);
    } finally { fixture.close(); }
  });
});

function scopePrivacyRow(goal: string, source: 'cli' | 'harness' = 'harness', owner: string | null = 'sess-test'): MetadataSummary {
  return { ...expertSummary({ repo: '/repo', session_id: 'sess-test', source,
    job_ref: { job_id: source === 'cli' ? 'job_cli' : 'job_one', state_id: 'scope-store' } }, goal),
    ownership: { origin: owner ? 'marionette' : null, session_id: owner, project_id: null },
    stamp: owner ? 'known' : 'legacy_unknown' };
}

let scopePrivacyMetadata: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;

async function renderScopePrivacy(row: MetadataSummary) {
  const metadata = await expertMetadataFixture([row]);
  scopePrivacyMetadata = metadata;
  render(<metadata.Provider><SwarmPane /></metadata.Provider>);
  expect(metadata.store.getSnapshot().observations).toHaveLength(1);
  expect(mockSwarmLive).not.toHaveBeenCalled();
  expect(mockArtifacts).not.toHaveBeenCalled();
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
  return metadata;
}

describe("SwarmPane session scope before a project event", () => {
  beforeEach(() => {
    vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache();
    dispatchProjectSelected('');
  });
  afterEach(() => { scopePrivacyMetadata?.dispose(); scopePrivacyMetadata = undefined; });

  it("resolves the backend active session and keeps its running swarm visible", async () => {
    vi.mocked(api.sessions).mockResolvedValue([
      { id: 'sess-test', title: 'Active chat', active: true, created: 1 },
    ]);
    // The app supplies the resolved session to its single metadata owner.
    const sessions = await api.sessions(undefined);
    const active = sessions.find(session => session.active);
    expect(active?.id).toBe('sess-test');
    const row = scopePrivacyRow('Running before project event');
    if (!active) throw Error('Missing backend active session');
    row.selection.session_id = active.id;
    const metadata = await renderScopePrivacy(row);
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter swarms' }), { target: { value: 'session' } });
    expect(await screen.findByRole('button', { name: /Running before project event.*running/ })).toBeVisible();
    expect(metadata.store.getSnapshot().view).toMatchObject({ context: { session_id: active.id } });
    expect(api.sessions).toHaveBeenCalledWith(undefined);
  });

  it("ignores the bootstrap response after a project selection", async () => {
    const metadata = await renderScopePrivacy(scopePrivacyRow('Bootstrap running swarm'));
    const original = metadata.request.getMockImplementation();
    if (!original) throw Error('Missing bounded transport');
    let finish: (value: unknown) => void = () => {};
    const bootstrap = await original('GET', '/api/jobs/metadata/view');
    metadata.request.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    let pending: Promise<unknown>;
    await act(async () => { pending = metadata.store.refreshView(); });
    const selected = scopePrivacyRow('Selected project running swarm');
    selected.selection = { ...selected.selection, repo: '/repo-selected', session_id: 'sess-selected' };
    selected.ownership.session_id = 'sess-selected';
    await act(async () => { dispatchProjectSelected('/repo-selected'); });
    await act(async () => { metadata.store.setTarget({ repo: '/repo-selected', session_id: 'sess-selected', scope: 'all' }); });
    await act(async () => { finish(bootstrap); await pending; });
    expect(metadata.store.getSnapshot().view).toMatchObject({ kind: 'target', target: { repo: '/repo-selected', session_id: 'sess-selected' } });
    await metadata.replace([selected]);
    expect(await screen.findByRole('button', { name: /Selected project running swarm/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /Selected project running swarm/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: /Bootstrap running swarm/ })).toBeNull();
    expect(metadata.store.getSnapshot().view).toMatchObject({ context: { repo: '/repo-selected', session_id: 'sess-selected' } });
  });
});

describe("SwarmPane tracker header", () => {
  beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache(); });
  afterEach(() => { scopePrivacyMetadata?.dispose(); scopePrivacyMetadata = undefined; });
  it("keeps the header free of session token/cost rollups", async () => {
    // Poison the retired feed so an accidental reconnection cannot pass unnoticed.
    mockSwarmLive.mockResolvedValue(liveJob({}, { tokens_used: 28_510_956, est_cost_usd: 26.9602, routing_saved_usd: 0.12 }));
    await renderScopePrivacy(scopePrivacyRow('Audit auth flow'));
    expect(await screen.findByRole('button', { name: /Audit auth flow/ })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Jobs' })).toBeVisible();
    expect(screen.queryByText(/28,510,956t/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$26\.9602/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^session$/)).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Estimated token usage, cost, and savings for this project/)).not.toBeInTheDocument();
  });
});

describe("SwarmPane does not paint unowned CLI captions", () => {
  beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache(); });
  afterEach(() => { scopePrivacyMetadata?.dispose(); scopePrivacyMetadata = undefined; });
  it("hides an unstamped CLI leak", async () => {
    await renderScopePrivacy(scopePrivacyRow('Cursor MCP implement', 'cli', null));
    expect(screen.getByRole('region', { name: 'Jobs' })).toBeVisible();
    expect(screen.queryByText(/Cursor MCP implement/)).toBeNull();
  });
  it("does not label an owned CLI row as external", async () => {
    await renderScopePrivacy(scopePrivacyRow('Cursor MCP implement', 'cli'));
    await expandJob(/Cursor MCP implement/);
    expect(screen.queryByTitle('Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace')).toBeNull();
    expect(screen.queryByText('visibility only')).toBeNull();
    expect(screen.queryByText('external')).toBeNull();
  });
  it("does not show an origin chip for harness jobs", async () => {
    const row = scopePrivacyRow('Audit auth flow');
    row.selection.job_ref = { ...row.selection.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' };
    const metadata = await renderScopePrivacy(row);
    metadata.selected.mockImplementation(async selected => capturedModelDetail(selected, metadata.context()));

    await expandJob(/Audit auth flow/);

    expect(
      screen.queryByTitle(
        "Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace",
      ),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
    fireEvent.click(await screen.findByRole("button", { name: "Routing", exact: true }));
    await waitFor(() => {
      expect(screen.getByTitle("Model: grok-4-5")).toBeInTheDocument();
      expect(screen.getByTitle("Model: grok-4-5")).toHaveTextContent("Historical model; current job and worker model unconfirmed");
      expect(screen.getByTitle("Model: grok-4-5").parentElement).toHaveTextContent("Captured attempt attempt-wiremodel: grok-4-5 · Historical model; current job and worker model unconfirmed.Task: task_34ac516de1b7. Run: run-wire.");
      expect(screen.getByRole("region", { name: "Routing", exact: true })).toContainElement(screen.getByTitle("Model: grok-4-5"));
    });
  });


  it("hides an unstamped cross-project leak", async () => {
    const metadata = await renderScopePrivacy(scopePrivacyRow('Foreign swarm', 'cli', null));
    const request = metadata.request.getMockImplementation();
    if (!request) throw Error('Missing metadata wire');
    metadata.request.mockImplementation(async (method, path) => {
      const value = await request(method, path);
      if (path.includes('/metadata/view')) return { ...view(), context: { repo: metadata.context().repo, session_id: metadata.context().session_id, view_generation: metadata.context().view_generation },
        sources: [{ source: 'cli', state_id: 'scope-store', cross_project: true, available: true }] };
      return value;
    });
    await act(async () => { await metadata.store.readView(); });
    expect(metadata.store.getSnapshot().view).toMatchObject({ view: { sources: [expect.objectContaining({ cross_project: true })] } });
    expect(screen.queryByText(/Foreign swarm/)).toBeNull();
  });
  it("does not disclose a cross-project cwd caption", async () => {
    const metadata = await renderScopePrivacy(scopePrivacyRow('Foreign swarm', 'cli', 'sess-9'));
    const request = metadata.request.getMockImplementation();
    if (!request) throw Error('Missing metadata wire');
    metadata.request.mockImplementation(async (method, path) => {
      const value = await request(method, path);
      if (path.includes('/metadata/view')) return { ...view(), context: { repo: metadata.context().repo, session_id: metadata.context().session_id, view_generation: metadata.context().view_generation },
        sources: [{ source: 'cli', state_id: 'scope-store', cross_project: true, available: true }] };
      return value;
    });
    await act(async () => { await metadata.store.readView(); });
    expect(metadata.store.getSnapshot().view).toMatchObject({ view: { sources: [expect.objectContaining({ cross_project: true })] } });
    await expandJob(/Foreign swarm/);
    expect(screen.queryByTitle('/Users/x/other-repo')).toBeNull();
    expect(screen.queryByText('visibility only')).toBeNull();
  });
});

describe("SwarmPane repo-scoped dismiss", () => {
  const REPO_A = "C:\\Users\\pwall\\Projects\\repo-a";
  const REPO_B = "C:\\Users\\pwall\\Projects\\repo-b";
  const SESSION = "sess-test";
  const hiddenMessage = "Observed finished jobs are hidden. Show hidden jobs to restore them.";
  let metadata: Awaited<ReturnType<typeof expertMetadataFixture>>;

  function row(id: string, goal: string, lifecycle = "complete", repo = REPO_A): MetadataSummary {
    return { ...expertSummary({ repo, session_id: SESSION, source: "harness",
      job_ref: { job_id: `job_${id}`, state_id: "state_fixture", version: 2, incarnation: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" } }, goal), lifecycle };
  }
  function preferenceKey(repo = REPO_A) {
    return `pmharness.metadata.jobs:${JSON.stringify([repo, SESSION])}`;
  }
  function dismissRows(rows: MetadataSummary[]) {
    localStorage.setItem(preferenceKey(), JSON.stringify({ expanded: [], dismissed: rows.map(r => metadataSelectionKey(r.selection)) }));
  }
  function mount() {
    return render(<metadata.Provider><SwarmPane /></metadata.Provider>);
  }
  function hideFinished() {
    fireEvent.click(screen.getByRole("button", { name: "Hide finished", exact: true }));
  }
  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    metadata = await expertMetadataFixture([row("shared-job", "Repo A finished swarm")]);
  });
  afterEach(() => {
    cleanup();
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
    metadata.dispose();
  });

  it("keeps dismiss state scoped to the active repo", async () => {
    mount();
    expect(await screen.findByRole("button", { name: /^Repo A finished swarm/ })).toBeInTheDocument();
    hideFinished();
    expect(await screen.findByText(hiddenMessage)).toBeInTheDocument();

    await metadata.replace([row("shared-job", "Repo B finished swarm", "complete", REPO_B)]);
    expect(await screen.findByRole("button", { name: /^Repo B finished swarm/ })).toBeInTheDocument();
    expect(screen.queryByText(hiddenMessage)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Repo A finished swarm/ })).not.toBeInTheDocument();
    await metadata.replace([row("shared-job", "Repo A finished swarm")]);
    expect(await screen.findByText(hiddenMessage)).toBeInTheDocument();
  });

  it("persists dismissed ids per repo across remounts", async () => {
    const { unmount } = mount();
    expect(await screen.findByRole("button", { name: /^Repo A finished swarm/ })).toBeInTheDocument();
    hideFinished();
    expect(await screen.findByText(hiddenMessage)).toBeInTheDocument();
    unmount();
    await metadata.observe();
    mount();
    expect(await screen.findByText(hiddenMessage)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Repo A finished swarm/ })).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(preferenceKey()) || "{}").dismissed)
      .toEqual([metadataSelectionKey(row("shared-job", "Repo A finished swarm").selection)]);
    expect(localStorage.getItem(preferenceKey(REPO_B))).toBeNull();
  });

  it("ignores the legacy global dismiss blob without deleting it", async () => {
    localStorage.setItem("swarm.dismissed.v1", JSON.stringify(["job_legacy-job"]));
    await metadata.replace([row("legacy-job", "Legacy finished swarm")]);
    mount();
    expect(await screen.findByRole("button", { name: /^Legacy finished swarm/ })).toBeInTheDocument();
    expect(screen.queryByText(hiddenMessage)).not.toBeInTheDocument();
    expect(localStorage.getItem("swarm.dismissed.v1")).toBe(JSON.stringify(["job_legacy-job"]));
    expect(JSON.parse(localStorage.getItem(preferenceKey()) || "{}").dismissed).toEqual([]);
  });

  it("keeps live jobs visible even when their id is in the dismiss store", async () => {
    const live = row("live-owned-job", "Owned swarm still running", "running");
    const finished = row("old-finished", "Previously cleared finished job");
    dismissRows([live, finished]);
    await metadata.replace([live, finished]);
    mount();
    expect(await screen.findByRole("button", { name: /^Owned swarm still running/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Previously cleared finished job/ })).not.toBeInTheDocument();
    expect(screen.queryByText(hiddenMessage)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show 1 hidden" })).toBeInTheDocument();
  });

  it("keeps a previously dismissed job visible after it completes if it was seen live", async () => {
    const live = row("live-owned-job", "Owned swarm still running", "running");
    dismissRows([live]);
    await metadata.replace([live]);
    const { unmount } = mount();
    expect(await screen.findByRole("button", { name: /^Owned swarm still running/ })).toBeInTheDocument();
    await waitFor(() => expect(JSON.parse(localStorage.getItem(preferenceKey()) || "{}").dismissed)
      .not.toContain(metadataSelectionKey(live.selection)));
    unmount();
    await metadata.replace([{ ...row("live-owned-job", "Owned swarm completed"), revision: 2 }]);
    mount();
    expect(await screen.findByRole("button", { name: /^Owned swarm completed/ })).toBeInTheDocument();
  });

  it("surfaces a newly completed job that was never dismissed after Clear", async () => {
    const old = row("old-a", "Old finished A");
    await metadata.replace([old]);
    const { unmount } = mount();
    expect(await screen.findByRole("button", { name: /^Old finished A/ })).toBeInTheDocument();
    hideFinished();
    expect(await screen.findByText(hiddenMessage)).toBeInTheDocument();
    unmount();
    await metadata.replace([old, row("new-peel", "MCP surface audit peel")]);
    mount();
    expect(await screen.findByRole("button", { name: /^MCP surface audit peel/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Old finished A/ })).not.toBeInTheDocument();
  });
});

describe("SwarmPane harness-open-swarm-job deep-link", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\deep-link";
  const selection: MetadataSelection = { repo: REPO, session_id: 'sess-test', source: 'harness',
    job_ref: { job_id: 'job_abcdef012345', state_id: 'state_fixture', version: 2,
      incarnation: '12345678-1234-4234-8234-123456789abc' } };
  const preferenceKey = `pmharness.metadata.jobs:${JSON.stringify([REPO, selection.session_id])}`;
  let metadata: Awaited<ReturnType<typeof expertMetadataFixture>>;

  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    const { clearPendingSwarmOpenJob } = await import("../lib/pendingSwarmOpenJob");
    clearPendingSwarmOpenJob();
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    metadata?.dispose();
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
  });
  async function fixture(goal: string, dismissed = false) {
    metadata = await expertMetadataFixture([{ ...expertSummary(selection, goal), lifecycle: 'complete' }]);
    metadata.selected.mockImplementation(async () => {
      const detail = expertDetail(selection, metadata.context());
      return { ...detail, lifecycle: 'complete', artifacts: { ...detail.artifacts,
        rows: [{ ...detail.artifacts.rows[0], id: 'artifact-target' }] } };
    });
    localStorage.setItem(preferenceKey, JSON.stringify({ expanded: [],
      dismissed: dismissed ? [metadataSelectionKey(selection)] : [] }));
  }
  function mount() { render(<metadata.Provider><SwarmPane /></metadata.Provider>); }
  function open(artifactId?: string) {
    act(() => { window.dispatchEvent(new CustomEvent('harness-open-swarm-job', {
      detail: { jobId: selection.job_ref.job_id, artifactId },
    })); });
  }
  async function expectExpanded(goal: string) {
    const row = await screen.findByRole('button', { name: `${goal} · complete` });
    expect(row).toHaveAttribute('aria-expanded', 'true');
    expect(row).toHaveFocus();
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.contexts).toContain(row);
    return row;
  }
  it("undismisses, expands, and scrolls to the target job row", async () => {
    await fixture('Deep-link target swarm', true);
    mount();
    expect(screen.getByRole('button', { name: 'Show 1 hidden' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Deep-link target swarm/ })).not.toBeInTheDocument();
    open();
    await expectExpanded('Deep-link target swarm');
    expect(document.querySelector('[data-job-id="job_abcdef012345"]')).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(preferenceKey) || '{}').dismissed).not.toContain(metadataSelectionKey(selection));
    fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
    await screen.findByRole('region', { name: 'Selected job inspector' });
    expect(metadata.selected).toHaveBeenCalledWith(selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
  });
  it("opens and scrolls to an exact artifact target", async () => {
    await fixture('Artifact deep-link target');
    mount();
    expect(screen.getByRole('button', { name: /Finished/ })).toBeInTheDocument();
    open('artifact-target');
    await waitFor(() => {
      const finding = document.querySelector('[data-artifact-ids~="artifact-target"]');
      expect(finding).toHaveAttribute('data-finding-id', 'artifact-target');
      expect(finding).toHaveAttribute('open');
      expect(finding).toHaveFocus();
      expect(vi.mocked(Element.prototype.scrollIntoView).mock.contexts).toContain(finding);
    });
    expect(screen.getByRole('button', { name: 'Artifacts', exact: true })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument();
    expect(metadata.selected).toHaveBeenCalledWith(selection, expect.objectContaining({ artifact_cursor: null }));
    expect(peekPendingSwarmNavigation()).toBeNull();
  });
  it("clears filters that hide a deep-link target", async () => {
    await fixture('Deep-link target behind filter');
    mount();
    fireEvent.change(screen.getByLabelText('Filter swarms'), { target: { value: 'active' } });
    expect(screen.queryByRole('button', { name: /Deep-link target behind filter/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Filter swarms')).toHaveValue('active');
    open();
    await expectExpanded('Deep-link target behind filter');
    expect(screen.getByLabelText('Filter swarms')).toHaveValue('all');
  });
  it("consumes a pending open-job queued before mount", async () => {
    await fixture('Late-mount deep-link target', true);
    // The producer captures the selected identity and owner context before mounting the pane.
    queuePendingSwarmNavigation({ kind: 'pm', selection, jobId: selection.job_ref.job_id,
      context: { repo: REPO, session_id: selection.session_id, contextEpoch: metadata.store.getSnapshot().contextEpoch } });
    expect(peekPendingSwarmNavigation()?.jobId).toBe(selection.job_ref.job_id);
    mount();
    await expectExpanded('Late-mount deep-link target');
    expect(peekPendingSwarmNavigation()).toBeNull();
    expect(JSON.parse(localStorage.getItem(preferenceKey) || '{}').dismissed).not.toContain(metadataSelectionKey(selection));
  });
});

describe("SwarmPane final-review blockers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps multiple associated captures without promoting a final worker assignment", async () => {
    const fixture = await capturedRoutingFixture({ attempts: [
      { task: 'task-a', model: 'preview-capture', id: 'capture-0' },
      { task: 'task-a', model: 'final-associated-capture', id: 'capture-1' },
    ] });
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText('preview-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('final-associated-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).getAllByText(/Historical identity; current worker model unconfirmed/)).toHaveLength(2);
    expect(within(worker.details).getByText(/Captured attempt capture-0/)).toBeVisible();
    expect(within(worker.details).getByText(/Captured attempt capture-1/)).toBeVisible();
    const other = workerDetails('task-b');
    fireEvent.click(other.button);
    expect(within(other.details).getByText('task-b', { exact: true })).toBeVisible();
    expect(other.details).not.toHaveTextContent('Captured attempt');
    expect(other.details).not.toHaveTextContent('capture-');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-0')).toBeVisible();
    expect(within(routing).getByText('Captured attempt capture-1')).toBeVisible();
    expect(within(routing).getAllByText('Task: task-a. Run: run-task-a.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps zero-task captured attempts distinct without a job model assignment", async () => {
    const fixture = await capturedRoutingFixture({ tasks: [], attempts: [
      { task: 'unmatched-task', model: 'unmatched-capture', id: 'unmatched-1' },
      { task: 'unmatched-task', model: 'later-capture', id: 'unmatched-2' },
    ] });
    expect(screen.getByText('No tasks recorded.')).toBeVisible();
    expect(screen.getByRole('button', { name: /^Captured routing evidence/ })).not.toHaveTextContent('unmatched-capture');
    fireEvent.click(within(fixture.inspector).getByRole('button', { name: 'Routing', exact: true }));
    const routing = screen.getByRole('region', { name: 'Routing' });
    expect(within(routing).getByText('Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-1')).toBeVisible();
    expect(within(routing).getByText('Captured attempt unmatched-2')).toBeVisible();
    expect(within(routing).getAllByText('Task: unmatched-task. Run: run-unmatched-task.')).toHaveLength(2);
    expect(within(routing).getAllByText(/Historical model; current job and worker model unconfirmed/)).toHaveLength(2);
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("refreshes exact-task captured identity while task lifecycle stays fixed", async () => {
    const fixture = await capturedRoutingFixture();
    const worker = workerDetails("task-a", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    const reads = fixture.selected.mock.calls.length;
    fixture.attempts([{ task: 'task-a', model: 'refreshed-capture', id: 'attempt-new' }]);
    await fixture.refresh();
    expect(fixture.selected.mock.calls.length).toBeGreaterThan(reads);
    expect(worker.button).toHaveTextContent('task-a: running');
    expect(within(worker.details).getByText('refreshed-capture', { exact: true })).toBeVisible();
    expect(within(worker.details).queryByText('captured-model', { exact: true })).not.toBeInTheDocument();
    expect(within(worker.details).getByText(/Captured attempt attempt-new.*current worker model unconfirmed/)).toBeVisible();
    const other = workerDetails('task-b');
    fireEvent.click(other.button);
    expect(within(other.details).getByText('role-task-b', { exact: true })).toBeVisible();
    expect(other.details).not.toHaveTextContent('refreshed-capture');
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("refreshes exact-task process outcomes without inventing failure diagnosis", async () => {
    const fixture = await capturedRoutingFixture({ status: 'failed', outcomes: [{ task: 'task-a', code: 1, timedOut: false }] });
    const worker = workerDetails("task-a", "failed");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("captured-model", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    expect(within(worker.details).getByText('1', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('false', { exact: true })).toBeVisible();
    const reads = fixture.selected.mock.calls.length;
    fixture.outcomes([{ task: 'task-a', code: 137, timedOut: true }]);
    await fixture.refresh();
    expect(fixture.selected.mock.calls.length).toBeGreaterThan(reads);
    expect(worker.button).toHaveTextContent('task-a: failed');
    expect(within(worker.details).getByText('137', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('true', { exact: true })).toBeVisible();
    expect(within(worker.details).queryByText('1', { exact: true })).not.toBeInTheDocument();
    expect(within(worker.details).getByText(/Historical process evidence; not a failure diagnosis/)).toBeVisible();
    expect(worker.details).not.toHaveTextContent('out of memory');
    const other = workerDetails('task-b', 'failed');
    fireEvent.click(other.button);
    expect(within(other.details).getByText('role-task-b', { exact: true })).toBeVisible();
    expect(other.details).not.toHaveTextContent('Captured process outcome');
    expect(other.details).not.toHaveTextContent('137');
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("does not render orphan receipt divider or green em dash on empty terminal jobs", async () => {
    const selection: MetadataSelection = { repo: "/repo", session_id: "sess-test", source: "harness",
      job_ref: { job_id: "job_empty_receipt", state_id: "store-A", version: 2,
        incarnation: "12345678-1234-4234-8234-123456789abc" } };
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selection, "Terminal without meters"), lifecycle: "complete", task_count: 0, artifact_count: 0,
    }]);
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      const job = await screen.findByRole("button", { name: /^Terminal without meters/ });
      expect(screen.getByRole("button", { name: /Finished \(1 observed\)/ })).toHaveAttribute("aria-expanded", "true");
      expect(job).not.toHaveTextContent("—");
      expect(job.textContent || "").not.toMatch(/\$0/);
      expect(job.querySelector('[aria-hidden="true"].bg-edge\\/70, [aria-hidden="true"][class*="bg-edge"]')).toBeNull();
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });

  it("still renders provider-attested $0 on terminal jobs when cost is known", async () => {
    const selection: MetadataSelection = { repo: "/repo", session_id: "sess-test", source: "harness",
      job_ref: { job_id: "job_zero_cost", state_id: "store-A", version: 2,
        incarnation: "22345678-1234-4234-8234-123456789abc" } };
    const metadata = await expertMetadataFixture([{
      ...expertSummary(selection, "Terminal with attested zero"), lifecycle: "complete",
    }]);
    const unknown: SelectedMetric = { total: null, state: "unknown", known_selected: 0,
      unknown_selected: 1, estimated_selected: 0, conflicting_selected: 0 };
    const providerZero: SelectedMetric = { total: 0, state: "measured", known_selected: 1,
      unknown_selected: 0, estimated_selected: 0, conflicting_selected: 0 };
    metadata.selected.mockResolvedValue({
      ...expertDetail(selection, metadata.context()), lifecycle: "complete",
      cost: { kind: "available", outcome: "available", job_ref: selection.job_ref,
        source: "terminal_receipt", coverage: "selected_receipt", summary_revision: 8,
        receipt_digest: "a".repeat(64), selected_count: 1, reason: null, retry_after_ms: null,
        totals: { tokens_in: unknown, tokens_out: unknown, cache_read_tokens: unknown,
          cache_write_tokens: unknown, api_cost_usd: providerZero,
          plan_marginal_cost_usd: unknown, api_equivalent_cost_usd: unknown } },
    });
    const rendered = render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    try {
      fireEvent.click(await screen.findByRole("button", { name: /^Terminal with attested zero/ }));
      fireEvent.click(screen.getByRole("button", { name: "Inspect tasks and artifacts" }));
      const inspector = await screen.findByRole("region", { name: "Selected job inspector" });
      fireEvent.click(within(inspector).getByRole("button", { name: "Economics", exact: true }));
      const cost = within(inspector).getByText("Selected API cost: $0 (measured).");
      expect(cost).toHaveTextContent("$0");
      expect(cost).not.toHaveTextContent("—");
      expect(within(inspector).getByText("Source: frozen terminal receipt. Selected records: 1.")).toBeVisible();
      expect(metadata.selected).toHaveBeenCalledWith(selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
    } finally {
      rendered.unmount();
      metadata.dispose();
    }
  });

  function renderInCompactRail(widthPx: number) {
    const { container } = render(
      <div style={{ width: `${widthPx}px`, overflow: "hidden" }}>
        <SwarmPane />
      </div>,
    );
    return container;
  }

  it("keeps historical identity disclosure breakable in a 320px rail", async () => {
    const fixture = await capturedRoutingFixture({ width: 320, tasks: ['task-test-coverage-reviewer-with-a-long-stable-task-identifier', 'task-b'], attempts: [{ task: 'task-test-coverage-reviewer-with-a-long-stable-task-identifier', model: 'provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', id: 'long-attempt' }] });
    const worker = workerDetails("task-test-coverage-reviewer-with-a-long-stable-task-identifier", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    expect(fixture.container.firstElementChild).toHaveStyle({ width: '320px' });
    const value = within(worker.details).getByText('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', { exact: true });
    expect(value.closest('dl')).toHaveClass('break-all');
    expect(within(worker.details).getByText('task-test-coverage-reviewer-with-a-long-stable-task-identifier', { exact: true }).closest('dl')).toHaveClass('break-all');
    expect(worker.button).not.toHaveTextContent('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim');
    expect(worker.details).not.toHaveClass('truncate');
    fireEvent.click(worker.button);
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(within(worker.details).getByText('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', { exact: true })).toBeVisible();
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });

  it("keeps long captured model text breakable in a 220px rail", async () => {
    const fixture = await capturedRoutingFixture({ width: 220, tasks: ['task-test-coverage-reviewer-with-a-long-stable-task-identifier', 'task-b'], attempts: [{ task: 'task-test-coverage-reviewer-with-a-long-stable-task-identifier', model: 'provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', id: 'long-attempt' }] });
    const worker = workerDetails("task-test-coverage-reviewer-with-a-long-stable-task-identifier", "running");
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(worker.details).toBeVisible();
    expect(within(worker.details).getByText("provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim", { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('openrouter', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText('captured-provider', { exact: true })).toBeVisible();
    expect(within(worker.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
    expect(within(worker.details).getByText('Model, adapter and live progress unavailable.')).toBeVisible();
    expect(fixture.container.firstElementChild).toHaveStyle({ width: '220px' });
    const value = within(worker.details).getByText('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', { exact: true });
    expect(value.closest('dl')).toHaveClass('break-all');
    expect(within(worker.details).getByText('task-test-coverage-reviewer-with-a-long-stable-task-identifier', { exact: true }).closest('dl')).toHaveClass('break-all');
    expect(worker.button).not.toHaveTextContent('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim');
    expect(worker.details).not.toHaveClass('truncate');
    fireEvent.click(worker.button);
    expect(worker.details).not.toBeVisible();
    fireEvent.click(worker.button);
    expect(within(worker.details).getByText('provider/model-with-a-very-long-captured-identifier-that-must-remain-verbatim', { exact: true })).toBeVisible();
    expect(fixture.selected).toHaveBeenCalledWith(fixture.selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled();
  });
});

describe("SwarmPane job-card expansion persistence", () => {
  const REPO_A = "C:\\Users\\pwall\\Projects\\repo-a";
  const REPO_B = "C:\\Users\\pwall\\Projects\\repo-b";

  let fixture: Awaited<ReturnType<typeof expertMetadataFixture>>;
  afterEach(() => fixture?.dispose());

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("remembers collapsed in_progress job across remount", async () => {
    fixture = await expertMetadataFixture([expansionSummary('job_running', 'Running swarm')]);

    const { unmount } = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    const job = await screen.findByRole("button", { name: /Running swarm/ });
    expect(job).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(job);
    expect(job).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(job);
    expect(job).toHaveAttribute("aria-expanded", "false");

    unmount();
    await fixture.observe();
    render(<fixture.Provider><SwarmPane /></fixture.Provider>);

    const remounted = await screen.findByRole("button", { name: /Running swarm/ });
    expect(remounted).toHaveAttribute("aria-expanded", "false");
  });

  it("remembers expanded terminal job across remount", async () => {
    fixture = await expertMetadataFixture([expansionSummary('job_done', 'Finished swarm job', '/repo', 'complete')]);

    const { unmount } = render(<fixture.Provider><SwarmPane /></fixture.Provider>);

    const job = await screen.findByRole("button", { name: /Finished swarm job/, expanded: false });
    expect(job).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(job);
    expect(job).toHaveAttribute("aria-expanded", "true");

    unmount();
    await fixture.observe();
    render(<fixture.Provider><SwarmPane /></fixture.Provider>);


    const remounted = await screen.findByRole("button", { name: /Finished swarm job/, expanded: true });
    expect(remounted).toHaveAttribute("aria-expanded", "true");
  });

  it("scopes collapse preference per repo", async () => {
    const rowA = expansionSummary('job_shared', 'Repo A running', REPO_A);
    const rowB = expansionSummary('job_shared', 'Repo B running', REPO_B);
    fixture = await expertMetadataFixture([rowA]);

    render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    const jobA = await screen.findByRole("button", { name: /Repo A running/ });
    expect(jobA).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(jobA);
    expect(jobA).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(jobA);
    expect(jobA).toHaveAttribute("aria-expanded", "false");

    await fixture.replace([rowB]);

    const jobB = await screen.findByRole("button", { name: /Repo B running/ });
    expect(jobB).toHaveAttribute("aria-expanded", "false");

    expect(JSON.parse(localStorage.getItem(expansionStorageKey(REPO_A)) || '{}').expanded).toEqual([]);
    fireEvent.click(jobB);
    expect(jobB).toHaveAttribute('aria-expanded', 'true');
    expect(JSON.parse(localStorage.getItem(expansionStorageKey(REPO_B)) || '{}').expanded).toEqual([metadataSelectionKey(rowB.selection)]);
    await fixture.replace([rowA]);
    expect(await screen.findByRole('button', { name: /Repo A running/ })).toHaveAttribute('aria-expanded', 'false');
  });

  it("falls back to defaults when expansion storage is malformed", async () => {
    localStorage.setItem(expansionStorageKey(), "not-json{{{");
    fixture = await expertMetadataFixture([expansionSummary('job_running', 'Running swarm')]);

    render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    const job = await screen.findByRole("button", { name: /Running swarm/ });
    expect(job).toHaveAttribute("aria-expanded", "false");
  });
});

describe("receipt-first spend (#102)", () => {
  it("names job identifiers and never uses routing forecast as worker spend", () => {
    expect(jobIdentifier("local-e0d790a9")).toBe("Job local-e0d790a9");
    expect(jobIdentifier("job_41123fa7ff1b")).toBe("Job job_41123fa7ff1b");
    expect(namedSavings(0.0278)).toBe("Estimated savings ~$0.0278");

    const job = {
      id: "local-e0d790a9",
      goal: "x",
      status: "complete",
      est_cost_usd: 0.1095,
      estimated: true,
      tokens: 10948,
      tasks: [{ id: "local-e0d790a9-w0", role: "implement", instruction: "", status: "done", adapter: "agentic" }],
    } as Job;
    const spend = workerSpend(job.tasks![0], job);
    expect(spend?.cost).toBeCloseTo(0.1095, 6);
    expect(spend?.basis).toBe("estimated");
  });

  it("does not invent a split for multi-worker jobs without task receipts", () => {
    const job = {
      id: "local-multi",
      goal: "x",
      status: "complete",
      est_cost_usd: 0.40,
      estimated: true,
      tasks: [
        { id: "w0", role: "a", instruction: "", status: "done", adapter: "agentic" },
        { id: "w1", role: "b", instruction: "", status: "done", adapter: "agentic" },
      ],
    } as Job;
    expect(workerSpend(job.tasks![0], job)).toBeNull();
    expect(workerSpend(job.tasks![1], job)).toBeNull();
  });
});

describe("swarm tracker usage pills (0.9.300)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("discloses selected PM receipt economics only within the expanded inspector", async () => {
    const fixture = await economicsReceiptFixture();
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      expect(screen.queryByRole('region', { name: 'Economics' })).not.toBeInTheDocument();
      await expandJob(/^Selected receipt audit ·/);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByRole('region', { name: 'Selected job inspector' });
      expect(screen.queryByText(/Selected plan marginal cost/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Economics', exact: true }));
      const economics = screen.getByRole('region', { name: 'Economics' });
      expect(economics).toHaveTextContent('Input tokens: 12 (measured). Output tokens: 0 (measured).');
      expect(economics).toHaveTextContent('Selected plan marginal cost: $0 (estimated).');
      expect(economics).toHaveTextContent('Selected API cost: unknown.');
      expect(economics).toHaveTextContent('Source: frozen terminal receipt. Selected records: 1.');
      expect(economics).toHaveTextContent('All-attempt spend: unknown');
      fireEvent.click(screen.getByRole('button', { name: /^Selected receipt audit ·/ }));
      expect(screen.queryByRole('region', { name: 'Economics' })).not.toBeInTheDocument();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { mounted.unmount(); fixture.dispose(); }
  });

  it("keeps captured task usage hidden until its observation is disclosed", async () => {
    const fixture = await economicsReceiptFixture();
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      await expandJob(/^Selected receipt audit ·/);
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      await screen.findByRole('region', { name: 'Tasks' });
      expect(screen.queryByText('tokens in')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'History', exact: true }));
      const observation = screen.getByText('observation 4').closest('details');
      if (!observation) throw Error('Missing captured observation disclosure');
      expect(observation).not.toHaveAttribute('open');
      expect(within(observation).getByText('tokens in')).not.toBeVisible();
      fireEvent.click(screen.getByText('observation 4'));
      expect(within(observation).getByText('tokens in')).toBeVisible();
      expect(within(observation).getByText('12')).toBeVisible();
      expect(within(observation).getByText('plan_marginal')).toBeVisible();
      expect(within(observation).getByText(fixture.detail.tasks.rows[0].id)).toBeVisible();
      expect(screen.getByText(/Captured records only/)).toBeVisible();
      fireEvent.click(screen.getByText('observation 4'));
      expect(within(observation).getByText('tokens in')).not.toBeVisible();
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { mounted.unmount(); fixture.dispose(); }
  });

  it("still shows grouped Findings count unchanged", async () => {
    const selected = metadataSelection();
    const fixture = await expertMetadataFixture([
      { ...expertSummary(selected, 'Grouped findings pill chrome'), lifecycle: 'complete', artifact_count: 3 },
    ]);
    const detail = expertDetail(selected, fixture.context());
    detail.lifecycle = 'complete';
    detail.artifact_count = 3;
    detail.artifacts.page.scanned = 3;
    detail.artifacts.rows = ['a', 'b', 'a'].map((digest, index) => ({
      id: `finding-${index}`, status: 'completed', stamp: 'known', revision: 3,
      task_id: 'task-1', type: 'finding', sha256: digest.repeat(64),
      presence: 'recorded', check_result: 'unavailable',
    }));
    fixture.selected.mockResolvedValue(detail);
    const mounted = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    try {
      await expandJob('Grouped findings pill chrome · complete');
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      expect(await screen.findByText('Findings (2)')).toBeInTheDocument();
      expect(screen.queryByText(/Findings \(2 of/)).toBeNull();
      expect(fixture.selected).toHaveBeenCalledTimes(1);
      expect(mockSwarmLive).not.toHaveBeenCalled();
      expect(mockArtifacts).not.toHaveBeenCalled();
      fixture.selected.mockResolvedValue({ ...detail, artifact_count: 4 });
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      expect(await screen.findByText('Findings (2 on this page)')).toBeInTheDocument();
      expect(screen.queryByText('Findings (2)')).not.toBeInTheDocument();
      fixture.selected.mockResolvedValue({ ...detail, artifacts: { ...detail.artifacts,
        rows: detail.artifacts.rows.map(row => ({ ...row, sha256: null })),
      } });
      fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
      expect(await screen.findByText('Findings (3)')).toBeInTheDocument();
    } finally {
      mounted.unmount();
      fixture.dispose();
    }
  });
});

describe("SwarmPane command vs swarm split", () => {
  let metadata: Awaited<ReturnType<typeof commandSplitFixture>> | undefined;
  afterEach(() => { metadata?.dispose(); metadata = undefined; });
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    mockSwarmCancel.mockResolvedValue({ ok: true } as any);
  });

  it("excludes run_command jobs from tracker count and cards", async () => {
    metadata = await commandSplitFixture(["Audit auth flow"], "run_command");
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    expect(await screen.findByRole("button", { name: "Audit auth flow · running" })).toBeInTheDocument();
    expect(screen.getByText("Swarm Tracker").parentElement).toHaveTextContent("(1 observed)");
    expect(screen.getByRole("button", { name: "Active (1 observed)" })).toBeVisible();
    expect(screen.queryByText("sleep 999")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Native activity (1 observed)" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Command · running" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("At least 2 active jobs");
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

  it("still shows run_swarm / run_implement / run_parallel cards", async () => {
    metadata = await commandSplitFixture(["run_swarm audit", "run_implement fix", "run_parallel wave"], "run_command_batch");
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    expect(await screen.findByRole("button", { name: "run_swarm audit · running" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "run_implement fix · running" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "run_parallel wave · running" })).toBeInTheDocument();
    expect(screen.getByText("Swarm Tracker").parentElement).toHaveTextContent("(3 observed)");
    expect(screen.getByRole("button", { name: "Active (3 observed)" })).toBeVisible();
    expect(screen.queryByText("echo batch")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Native activity (1 observed)" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Command batch · running" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("At least 4 active jobs");
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

  it("does not count a lone command job as Swarm Tracker (1)", async () => {
    metadata = await commandSplitFixture([], "run_command");
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    expect(screen.getByText("Swarm Tracker").parentElement).toHaveTextContent("(0 observed)");
    expect(screen.getByText("No swarm jobs observed in this view.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Active \(/ })).not.toBeInTheDocument();
    expect(screen.getByText("Swarm Tracker").parentElement).not.toHaveTextContent("(1)");
    expect(screen.queryByText("sleep 999")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Native activity (1 observed)" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Command · running" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("At least 1 active jobs");
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

  it("hides the wave parent and still shows hired children", async () => {
    metadata = await commandSplitFixture(["run_swarm audit", "run_implement fix"], "parallel_wave");
    render(<metadata.Provider><SwarmPane /></metadata.Provider>);
    expect(await screen.findByRole("button", { name: "run_swarm audit · running" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "run_implement fix · running" })).toBeInTheDocument();
    expect(screen.getByText("Swarm Tracker").parentElement).toHaveTextContent("(2 observed)");
    expect(screen.getByRole("button", { name: "Active (2 observed)" })).toBeVisible();
    expect(screen.queryByText("Parallel wave (2 jobs)")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Native activity (1 observed)" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Parallel wave · running" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("At least 3 active jobs");
    expect(mockSwarmLive).not.toHaveBeenCalled();
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

});

describe("SwarmPane v0.9.350 collapsed chrome", () => {
  let fixture: Awaited<ReturnType<typeof expertMetadataFixture>>;
  beforeEach(async () => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-25T12:00:00Z"));
    fixture = await expertMetadataFixture([{ ...expansionSummary('job_live', 'Live audit'), task_count: 2 }]);
  });

  afterEach(() => {
    fixture?.dispose();
    vi.useRealTimers();
  });

  it("keeps collapsed PM rows compact and hides identity and selected details", async () => {
    render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    const row = await screen.findByRole('button', { name: /Live audit/ });
    expect(row).toHaveClass('items-center');
    expect(row).not.toHaveClass('flex-col');
    expect(row).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('Job job_live')).not.toBeInTheDocument();
    expect(screen.queryByText(/m ago/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Workers')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Selected job inspector' })).not.toBeInTheDocument();
    expect(fixture.selected).not.toHaveBeenCalled();
    fireEvent.click(row);
    expect(screen.getByLabelText('Job job_live')).toBeVisible();
    fireEvent.click(row);
    expect(screen.queryByLabelText('Job job_live')).not.toBeInTheDocument();
  });

  it("discloses job identity and selected worker progress after expansion", async () => {
    const selected = fixture.store.getSnapshot().observations[0].row.selection;
    const detail = expertDetail(selected, fixture.context());
    detail.task_count = 2;
    detail.tasks.rows = [
      { ...detail.tasks.rows[0], status: 'completed' },
      { ...detail.tasks.rows[0], id: 'task-2', binding: null, status: 'running' },
    ];
    detail.tasks.page.scanned = 2;
    fixture.selected.mockResolvedValue(detail);
    render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    expect(screen.queryByLabelText('Workers')).not.toBeInTheDocument();
    await expandJob(/Live audit/);
    expect(screen.getByLabelText('Job job_live')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
    const workers = await screen.findByLabelText('Workers');
    expect(workers).toHaveTextContent('1/2 observed workers finished');
    expect(screen.getByText('2 tasks shown of 2. Page: complete.')).toBeVisible();
    expect(screen.queryByText(/1m ago/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Live audit/ }));
    expect(screen.queryByLabelText('Workers')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Job job_live')).not.toBeInTheDocument();
  });

  it("uses hairline separators between job rows, not card borders", async () => {
    await fixture.replace([
      expansionSummary('job_a', 'First job'), expansionSummary('job_b', 'Second job'),
    ]);
    render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    await screen.findByText("First job");
    const first = screen.getByText("First job").closest("[data-job-id]");
    const second = screen.getByText("Second job").closest("[data-job-id]");
    expect(first?.className || "").toMatch(/border-b/);
    expect(first?.className || "").not.toMatch(/rounded-md/);
    expect(second?.className || "").toMatch(/border-b/);
  });
});

describe("SwarmPane 353 mixed chrome and routing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.sessions).mockResolvedValue([]);
    localStorage.clear();
    localStorage.setItem("marionette.jobScope.v1", "repo");
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("counts degraded workers so parent chrome is not clean-green", () => {
    const job = {
      id: "job-mix",
      goal: "audit",
      status: "complete",
      tasks: [
        { id: "t1", status: "complete", adapter: "agentic", role: "impl", instruction: "" },
        { id: "t2", status: "complete", adapter: "agentic", role: "review", instruction: "" },
        { id: "t3", status: "complete", adapter: "agentic", role: "map", instruction: "" },
        { id: "t4", status: "complete", adapter: "agentic", role: "conflict", instruction: "" },
        { id: "t5", status: "complete", adapter: "agentic", role: "ok", instruction: "" },
      ],
      artifacts: [
        { type: "verification", headline: "d", task_id: "t1", result: "degraded" },
        { type: "verification", headline: "d", task_id: "t2", result: "degraded" },
        { type: "verification", headline: "d", task_id: "t3", result: "degraded" },
        { type: "verification", headline: "d", task_id: "t4", result: "degraded" },
      ],
    } as Job;
    expect(jobDegradedWorkerCount(job)).toBe(4);
  });

  it("keeps mixed terminal task progress and verification records neutral at job level", async () => {
    const fixture = await qualityFixture({ lifecycle: 'complete',
      tasks: [identityTask('task-a', 'complete'), identityTask('task-b', 'failed'), identityTask('task-c', 'cancelled')],
      models: ['model-a', 'model-b', 'model-c'], outcomes: [{ task: 'task-b', code: 2, timedOut: false }] }, 'task-a');
    try {
      expect(screen.getByLabelText('Workers')).toHaveTextContent('3/3 observed workers finished · 1 completed · 1 failed · 1 cancelled');
      const a = workerDetails('task-a', 'complete'); const b = workerDetails('task-b', 'failed'); const c = workerDetails('task-c', 'cancelled');
      fireEvent.click(a.button); fireEvent.click(b.button); fireEvent.click(c.button);
      expect(within(b.button).getByText('task-b: failed')).toHaveClass('text-risk');
      expect(within(c.button).getByText('task-c: cancelled')).toHaveClass('text-muted');
      expect(within(b.details).getByText('2')).toBeVisible();
      for (const worker of [a, c]) {
        expect(within(worker.details).queryByText(/Captured process outcome/)).not.toBeInTheDocument();
        expect(worker.button.querySelector('.text-risk, .text-good')).toBeNull();
      }
      expect(within(fixture.job).getByText('complete')).toHaveClass('text-muted');
      expect(fixture.job.querySelector('.text-good')).toBeNull();
      const card = fixture.job.closest('[data-job-id]');
      expect(card?.className).toMatch(/border-b/);
      expect(card?.className).not.toMatch(/rounded-md/);
      fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-record: recorded');
      expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('do not establish that checks passed');
      fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
      fireEvent.click(screen.getByText('verification / verification-record: complete'));
      expect(screen.getByText('task-a')).toBeVisible();
      expect(screen.getByText(evidenceHash)).toBeVisible();
      rejectQualityVerdicts(fixture.detail);
    } finally { fixture.close(); }
  });

  it("discloses captured model adapter provider and run identity only for the matching task", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('reviewer'), identityTask('implementer')],
      models: ['openai/gpt-5.6', 'captured-implementation-model'] });
    try {
      const reviewer = workerDetails('reviewer'); const implementer = workerDetails('implementer');
      expect(reviewer.details).not.toBeVisible();
      fireEvent.click(reviewer.button); fireEvent.click(implementer.button);
      expect(within(reviewer.details).getByText('openai/gpt-5.6')).toBeVisible();
      expect(within(reviewer.details).getByText('Captured adapter')).toBeVisible();
      expect(within(reviewer.details).getByText('openrouter')).toBeVisible();
      expect(within(reviewer.details).getByText('Captured provider')).toBeVisible();
      expect(within(reviewer.details).getByText('openai')).toBeVisible();
      expect(within(reviewer.details).getByText('role-reviewer')).toBeVisible();
      expect(within(reviewer.details).getByText('worker-reviewer')).toBeVisible();
      expect(within(reviewer.details).getByText('run-0')).toBeVisible();
      expect(within(reviewer.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
      expect(within(implementer.details).getByText('captured-implementation-model')).toBeVisible();
      expect(within(implementer.details).queryByText('openai/gpt-5.6')).not.toBeInTheDocument();
      expect(within(reviewer.details).queryByText('captured-implementation-model')).not.toBeInTheDocument();
      expect(reviewer.button).not.toHaveTextContent('openai/gpt-5.6');
      expect(screen.getByLabelText('Workers')).toHaveTextContent('0/2 observed workers finished');
      fireEvent.click(screen.getByRole('button', { name: 'Routing', exact: true }));
      const routing = screen.getByRole('region', { name: 'Routing' });
      expect(routing).toHaveTextContent('Captured attempt attempt-0');
      expect(routing).toHaveTextContent('Task: reviewer. Run: run-0');
      expect(routing).toHaveTextContent('model: openai/gpt-5.6');
      expect(routing).toHaveTextContent('Historical model; current job and worker model unconfirmed');
      expect(routing).toHaveTextContent('Task: implementer. Run: run-1');
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { fixture.close(); }
  });

  it("refreshes task-specific captured models without presenting them as current assignments", async () => {
    const fixture = await identityFixture({ tasks: [identityTask('impl'), identityTask('review')], models: ['captured-old-model', 'stable-review-model'] });
    try {
      const impl = workerDetails('impl'); const review = workerDetails('review');
      fireEvent.click(impl.button); fireEvent.click(review.button);
      expect(within(impl.details).getByText('captured-old-model')).toBeVisible();
      expect(within(review.details).getByText('stable-review-model')).toBeVisible();
      fixture.models(['gpt-5.3-codex', 'stable-review-model']);
      fireEvent.click(fixture.inspect);
      expect(await within(impl.details).findByText('gpt-5.3-codex')).toBeVisible();
      expect(within(impl.details).queryByText('captured-old-model')).not.toBeInTheDocument();
      expect(within(impl.details).getByText('Captured model')).toBeVisible();
      expect(within(impl.details).getByText('openrouter')).toBeVisible();
      expect(within(impl.details).getByText('run-0')).toBeVisible();
      expect(within(impl.details).getByText(/Historical identity; current worker model unconfirmed/)).toBeVisible();
      expect(within(review.details).getByText('stable-review-model')).toBeVisible();
      expect(within(review.details).queryByText('gpt-5.3-codex')).not.toBeInTheDocument();
      expect(impl.button).not.toHaveTextContent('gpt-5.3-codex');
      const selected = fixture.store.getSnapshot().detail;
      if (selected.kind !== 'selected' || !selected.observation) throw Error('Missing current selected detail');
      const detail = selected.observation;
      expect(parseMetadataDetail(detail, detail.context, detail.selection, selected.cursors).tasks.rows[0].id).toBe('impl');
      expect(() => parseMetadataDetail({ ...detail, tasks: { ...detail.tasks,
        rows: [{ ...detail.tasks.rows[0], model: 'gpt-5.3-codex' }] } }, detail.context, detail.selection, selected.cursors)).toThrow('invalid_metadata');
      fireEvent.click(screen.getByRole('button', { name: 'Routing', exact: true }));
      expect(screen.getByRole('region', { name: 'Routing' })).toHaveTextContent('model: gpt-5.3-codex');
      expect(screen.getByRole('region', { name: 'Routing' })).toHaveTextContent('Task: impl. Run: run-0');
      expect(screen.getByRole('region', { name: 'Routing' })).toHaveTextContent('Historical model; current job and worker model unconfirmed');
      expect(fixture.selected).toHaveBeenCalledTimes(2);
      expect(mockSwarmLive).not.toHaveBeenCalled();
    } finally { fixture.close(); }
  });
});
