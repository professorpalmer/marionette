import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { context, summary, view } from './jobMetadata.fixtures';
import { nativeSummary } from './metadataMigration.fixtures';
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getWorkspace: vi.fn().mockResolvedValue({
      repo: "/workspace",
      branch: "main",
      is_git: true,
      head_unborn: false,
      codegraph_status: "ready",
      recents: [],
      home: "/home",
    }),
    workspaces: vi.fn().mockResolvedValue([
      { name: "main", active: true, dirty: false },
    ]),
    sessions: vi.fn().mockResolvedValue([
      { id: "session-1", title: "Current", active: true, repo: "/workspace" },
    ]),
    jobs: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({
  useOperationalDiagnostic: () => null,
}));

describe("LeftRail branch layout", () => {
  beforeEach(() => {
    localStorage.clear();
    clearSWRCache();
  });

  it("gives the branches list a fixed height so the resize handle can move", async () => {
    const { container } = render(<LeftRail jobsRefresh={0} />);

    await screen.findByRole("button", { name: /main/ });
    const branchList = container.querySelector<HTMLElement>("[data-slot=left-rail-branches-list]");
    const upperSections = container.querySelector<HTMLElement>("[data-slot=left-rail-upper-sections]");
    const jobsPanel = container.querySelector<HTMLElement>("[data-slot=left-rail-jobs]");
    const jobScopes = container.querySelector<HTMLElement>("[data-slot=left-rail-job-scopes]");
    expect(screen.getByRole("button", { name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("separator", { name: "Resize branches list" })).toBeInTheDocument();
    expect(branchList?.style.height).not.toBe("");
    expect(branchList?.style.maxHeight).toBe("");
    expect(upperSections?.className.split(" ")).not.toContain("flex-1");
    expect(jobsPanel).toHaveClass("mt-auto");
    expect(jobScopes).toHaveClass("grid", "grid-cols-3");
  });
});

it('keeps observed active PM and native jobs above finished history in the capped sidebar', async () => {
  localStorage.clear(); clearSWRCache();
  const store = new JobMetadataStore();
  const target = { ...context, repo: '/workspace', session_id: 'session-1' };
  const queued = summary();
  const snapshot = store.getSnapshot();
  const activeNative = { ...nativeSummary(100), session_id: target.session_id, kind: 'run_command', lifecycle: 'running' };
  const retained = Array.from({ length: 30 }, (_, i) => ({ ...nativeSummary(i + 1), session_id: target.session_id, kind: 'provider', lifecycle: 'completed' }));
  vi.spyOn(store, 'getSnapshot').mockReturnValue({ ...snapshot,
    view: { kind: 'view', target, context: target, view: { ...view(), context: target }, refresh: 'idle' },
    observations: [{ row: { ...queued, lifecycle: 'queued', ownership: { ...queued.ownership, session_id: target.session_id } }, freshness: 'observed' }],
    local: { ...snapshot.local, observations: [activeNative, ...retained].map(row => ({ row, freshness: 'observed', observedAt: 1 })) },
  });
  const mounted = render(<JobMetadataContext.Provider value={store}><LeftRail jobsRefresh={0} /></JobMetadataContext.Provider>);
  try {
    await screen.findByRole('button', { name: 'Show all (32)' });
    expect(screen.getByRole('button', { name: 'PM harness job', exact: true })).toBeVisible();
    expect(screen.getByRole('button', { name: 'run command', exact: true })).toBeVisible();
    const list = mounted.container.querySelector('[data-slot="left-rail-jobs"]');
    const labels = [...(list?.querySelectorAll('button') ?? [])].map(button => button.textContent?.trim());
    expect(labels.indexOf('PM harness job')).toBeLessThan(labels.indexOf('provider'));
    expect(labels.indexOf('run command')).toBeLessThan(labels.indexOf('provider'));
  } finally { mounted.unmount(); vi.restoreAllMocks(); store.dispose(); }
});
