import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ComposerActivityRail from '../components/conversation/ComposerActivityRail';
import { JobMetadataContext, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataClient } from '../lib/jobMetadata';
import { CombinedMetadataFixture } from './metadataMigration.fixtures';
import { context, list, summary } from './jobMetadata.fixtures';

const stores: JobMetadataStore[] = [];
beforeEach(() => {
  // jsdom does not implement the browser's top layer or dialog methods.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: vi.fn(function (this: HTMLDialogElement) { this.setAttribute('open', ''); }) });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: vi.fn(function (this: HTMLDialogElement) { this.removeAttribute('open'); }) });
});
afterEach(() => { cleanup(); stores.splice(0).forEach(store => store.dispose()); Reflect.deleteProperty(window, 'harnessIPC'); vi.restoreAllMocks(); });

function Rail({ sessionId, empty }: { sessionId: string; empty: boolean }) {
  const { state } = useSharedJobMetadata();
  return <div data-testid="composer-context" className="max-h-[25dvh] overflow-y-auto">
    <ComposerActivityRail jobs={empty ? [] : metadataJobs(state)} sessionId={sessionId} />
  </div>;
}
async function fixture() {
  const wire = new CombinedMetadataFixture(); wire.total = 1;
  let removed = false;
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (method: string, path: string, body: unknown) => {
      const url = new URL(path, 'http://fixture');
      if (removed && url.pathname === '/api/jobs/metadata') return wire.response({
        ...list([]), rows: [{ selection: { ...summary().selection, job_ref: { ...summary().selection.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } }, revision: 20000, deleted: true }], mode: url.searchParams.get('mode'),
        page: { outcome: 'complete', revision: 20000, checkpoint: 20000, scanned: 1, next_cursor: null },
      });
      return wire.request(method, path, body);
    },
  } });
  const store = new JobMetadataStore(new JobMetadataClient(1000)); stores.push(store);
  store.setTarget(context); await store.readView(); await store.advance();
  const tree = (empty = false, sessionId = context.session_id) => <JobMetadataContext.Provider value={store}><Rail empty={empty} sessionId={sessionId} /></JobMetadataContext.Provider>;
  const result = render(tree());
  return { store, wire, remove: () => { removed = true; wire.total = 0; }, rerender: (empty = false, sessionId = context.session_id) => result.rerender(tree(empty, sessionId)) };
}

it('retains the one selected inspector through empty rail input, including its selected read, and clears it on context ABA', async () => {
  const f = await fixture();
  const beforeOpen = f.wire.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'PM harness job · running' }));
  expect(f.wire.calls).toHaveLength(beforeOpen);
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const inspector = screen.getByRole('region', { name: 'Selected job inspection' });
  const task = await within(inspector).findByText('task-1: running');
  const taskIdentity = within(inspector).getByText('task-1', { exact: true });
  const selected = f.store.getSnapshot().detail;
  const afterInspection = f.wire.calls.length;
  f.rerender(true);
  expect(f.wire.calls).toHaveLength(afterInspection);
  expect(screen.getByRole('region', { name: 'Selected job inspection' })).toBe(inspector);
  expect(inspector).toBeVisible();
  expect(within(inspector).getByText('task-1: running')).toBe(task);
  expect(within(inspector).getByText('task-1', { exact: true })).toBe(taskIdentity);
  expect(task).toBeVisible();
  expect(f.store.getSnapshot().detail).toBe(selected);
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  act(() => f.store.setTarget(context));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(f.store.getSnapshot().detail.kind).toBe('none');
});

it('opens a labelled modal outside composer clipping and restores keyboard focus on close', async () => {
  const f = await fixture();
  const opener = screen.getByRole('button', { name: 'PM harness job · running' }); opener.focus();
  fireEvent.click(opener);
  const dialog = screen.getByRole('dialog', { name: 'Selected job inspection' });
  expect(dialog).toHaveAccessibleDescription('Inspect the selected job without leaving the conversation.');
  expect(screen.getByTestId('composer-context')).not.toContainElement(dialog);
  expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledTimes(1);
  const close = within(dialog).getByRole('button', { name: 'Close selected inspection' });
  expect(close).toHaveFocus();
  fireEvent.keyDown(close, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  fireEvent.click(opener); f.rerender(true);
  fireEvent.click(screen.getByRole('button', { name: 'Close selected inspection' }));
  expect(screen.getByRole('region', { name: 'Observed job activity' })).toHaveFocus();
  expect(f.store.getSnapshot().detail.kind).toBe('none');
});

it('closes selection immediately when the rail session changes before the provider effect', async () => {
  const f = await fixture();
  fireEvent.click(screen.getByRole('button', { name: 'PM harness job · running' }));
  f.rerender(false, 'different-session');
  expect(screen.queryByRole('region', { name: 'Selected job inspection' })).not.toBeInTheDocument();
});

it('preserves inspection when real store tombstones remove the last observed job', async () => {
  const f = await fixture();
  fireEvent.click(screen.getByRole('button', { name: 'PM harness job · running' }));
  const epoch = f.store.getSnapshot().contextEpoch;
  f.remove();
  for (let i = 0; i < 8; i++) await act(async () => { await f.store.advance(); });
  expect(metadataJobs(f.store.getSnapshot())).toEqual([]);
  expect(f.store.getSnapshot().contextEpoch).toBe(epoch);
  expect(screen.getByRole('dialog', { name: 'Selected job inspection' })).toBeVisible();
  expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { bubbles: false, cancelable: true }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Observed job activity' })).toHaveFocus();
});
