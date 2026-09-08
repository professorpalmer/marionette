import { act } from '@testing-library/react';
import nativeWire from './nativeExpert.backend.json';
import captured from './expertWire.backend.json';
import { expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { selection } from './jobMetadata.fixtures';
import { nativeActiveStatuses, nativeAttentionStatuses, parseLocalDetail } from '../lib/localJobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { parseMetadataDetail, parseMetadataSelection } from '../lib/jobMetadata';

export async function economicsNativeFixture(rows: LocalSummary[]) {
  const fixture = await expertMetadataFixture([expertSummary(selection(), 'Undated PM observation')]);
  const original = fixture.request.getMockImplementation();
  if (!original) throw Error('Missing metadata wire responder');
  let current = rows;
  let revision = Math.max(...rows.map(row => row.revision));
  fixture.request.mockImplementation(async (method, path) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname.endsWith('/view')) {
      const result = await original(method, path);
      if (!result || typeof result !== 'object') throw Error('Missing metadata view');
      return { ...result, local: { available: true, incarnation: nativeWire.tasks.local_ref.incarnation, version: 1,
        lanes: ['active', 'history'], active_statuses: nativeActiveStatuses, attention_statuses: nativeAttentionStatuses } };
    }
    if (url.pathname.includes('/metadata/local')) {
      const lane = url.searchParams.get('lane') ?? 'history';
      const row = current.find(row => row.local_ref.job_id === url.searchParams.get('job_id')) ?? current[0];
      if (url.pathname.endsWith('/detail')) return { ...nativeWire.tasks, context: fixture.context(), local_ref: row.local_ref,
        summary: row, lane, rows: [], total: 0, page: { ...nativeWire.tasks.page, revision, checkpoint: revision, scanned: 0 } };
      const listed = lane === 'active' ? current.filter(row => nativeActiveStatuses.includes(row.lifecycle)) : current;
      return { ...nativeWire.tasks, context: fixture.context(), incarnation: nativeWire.tasks.local_ref.incarnation, lane, rows: listed,
        coverage: { ...nativeWire.tasks.coverage, membership: lane === 'active' ? 'retained_local_active' : 'retained_local_history', ...(lane === 'active' ? { metadata: 'live_during_traversal' } : {}) },
        page: { ...nativeWire.tasks.page, revision, checkpoint: revision, scanned: listed.length } };
    }
    return original(method, path);
  });
  await act(async () => { await fixture.store.readView(); await fixture.store.advanceLocal('history'); });
  for (let i = 0; i < 16 && fixture.store.getSnapshot().observations.length === 0; i++) {
    await act(async () => { await fixture.store.advance(); });
  }
  return { ...fixture, async update(next: LocalSummary[]) {
    current = next; revision = Math.max(...next.map(row => row.revision));
    await act(async () => { await fixture.store.advanceLocal('history'); });
  } };
}

// Native fields are projected by harness/local_job_metadata.py:operator_facts and LocalMetadataIndex.project.
export function nativeEconomicsRow(id: string, lifecycle = 'running', createdAt: number | null = 100): LocalSummary {
  const parsed = parseLocalDetail(nativeWire.tasks, { ...nativeWire.tasks.context, scope: 'session' }, nativeWire.tasks.local_ref, 'tasks');
  if (!parsed.summary) throw Error('Producer capture needs a summary');
  return { ...parsed.summary, local_ref: { ...parsed.summary.local_ref, job_id: id }, session_id: selection().session_id,
    lifecycle, created_at: createdAt, display: { label: 'Provider worker', model: id, adapter: 'agentic', truncated: false },
    usage: { kind: 'reported', tokens: 12000, source: 'local_job_tokens' },
    economics: { kind: 'estimated', spend_usd: 0.05, estimated: true, cost_provenance: 'static', source: 'financial_receipt', estimated_savings_usd: 0.02 },
  };
}

export async function economicsReceiptFixture() {
  const raw = captured[0].detail;
  const selected = parseMetadataSelection({ ...selection(), job_ref: raw.selection.job_ref }, selection());
  const fixture = await expertMetadataFixture([{ ...expertSummary(selected, 'Selected receipt audit'), lifecycle: 'complete' }]);
  const detail = parseMetadataDetail({ ...raw, selection: selected, context: fixture.context() }, fixture.context(), selected, { task_cursor: null, artifact_cursor: null });
  fixture.selected.mockResolvedValue(detail);
  return { ...fixture, detail };
}
