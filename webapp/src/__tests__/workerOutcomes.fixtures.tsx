import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect } from 'vitest';
import SwarmPane from '../components/SwarmPane';
import type { MetadataSelection } from '../lib/jobMetadata';
import { context } from './jobMetadata.fixtures';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';

let fixture: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;
afterEach(() => { fixture?.dispose(); fixture = undefined; });

export async function renderWorkerMetadata(goal: string, lifecycle: string, statuses: string[]) {
  const selection: MetadataSelection = { repo: context.repo, session_id: context.session_id, source: 'harness',
    job_ref: { job_id: 'job_workers', state_id: 'store-A', version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
  const f = await expertMetadataFixture([{ ...expertSummary(selection, goal), lifecycle, task_count: statuses.length }]);
  fixture = f;
  f.selected.mockImplementation(async selected => {
    const detail = expertDetail(selected, f.context());
    return { ...detail, lifecycle, task_count: statuses.length, artifact_count: 0,
      tasks: { page: { ...detail.tasks.page, scanned: statuses.length }, rows: statuses.map((status, index) => ({
        id: `t${index + 1}`, status, stamp: 'known', revision: index + 1, binding: null,
      })) }, artifacts: { page: { ...detail.artifacts.page, scanned: 0 }, rows: [] } };
  });
  render(<f.Provider><SwarmPane /></f.Provider>);
  fireEvent.click(await screen.findByRole('button', { name: `${goal} · ${lifecycle}` }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await screen.findByRole('region', { name: 'Selected job inspector' });
  expect(f.selected).toHaveBeenCalledTimes(1);
  expect(f.store.getSnapshot().detail.kind).toBe('selected');
}
