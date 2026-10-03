import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { JobMetadataOwner, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import { context, handshake, list, response, view } from './jobMetadata.fixtures';

function Observed() {
  const { state } = useSharedJobMetadata();
  return <output>{metadataJobs(state).map(job => job.id).join(',')}</output>;
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('first open discovers existing PM jobs once without an operator refresh', async () => {
  vi.useFakeTimers();
  Reflect.deleteProperty(window, 'harnessIPC');
  let discovered = false;
  const calls: { method: string; path: string }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'http://fixture');
    const route = url.pathname;
    const method = init?.method ?? 'GET';
    calls.push({ method, path: route });
    if (route === '/api/endpoint') return response(handshake);
    if (route === '/api/jobs/metadata/view/refresh') {
      expect(method).toBe('POST');
      expect(JSON.parse(String(init?.body))).toEqual({ view_generation: context.view_generation });
      discovered = true;
      return response(view('generation-discovered'));
    }
    if (route === '/api/jobs/metadata/view') return response(discovered ? view('generation-discovered') : {
      ...view(), availability: 'unavailable', sources: [], missing: ['sources_not_refreshed'],
    });
    if (route === '/api/jobs/metadata/pins') {
      const body = JSON.parse(String(init?.body));
      expect(body.selections.length).toBeLessThanOrEqual(8);
      return response({ version: 1, context: { ...context, scope: 'all', view_generation: 'generation-discovered' }, results: body.selections.map((selection: unknown) => ({ selection, result: { kind: 'unavailable', reason: 'fixture' } })) });
    }
    if (route === '/api/jobs/metadata') {
      expect(discovered).toBe(true);
      const result = list();
      return response({ ...result, mode: url.searchParams.get('mode'), rows: result.rows.map(row => ({ ...row, lifecycle: url.searchParams.get('status') ?? 'running' })), context: { ...result.context, scope: 'all', view_generation: 'generation-discovered' } });
    }
    throw new Error(`Unexpected ${method} ${route}`);
  }));
  render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Observed /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(calls.filter(call => call.path === '/api/jobs/metadata/view/refresh')).toEqual([{ method: 'POST', path: '/api/jobs/metadata/view/refresh' }]);
  expect(calls.some(call => call.path === '/api/jobs/metadata/pins')).toBe(true);
  expect(screen.getByRole('status').textContent).toContain('job_1');
});

it('rediscovers once when a store is written after the view was discovered', async () => {
  vi.useFakeTimers();
  Reflect.deleteProperty(window, 'harnessIPC');
  let discovered = false;
  const calls: { method: string; path: string }[] = [];
  const late = { ...view(), sources: view().sources.map(s => ({ ...s, available: false })) };
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'http://fixture');
    const route = url.pathname;
    const method = init?.method ?? 'GET';
    calls.push({ method, path: route });
    if (route === '/api/endpoint') return response(handshake);
    if (route === '/api/jobs/metadata/view/refresh') { discovered = true; return response(view('generation-discovered')); }
    if (route === '/api/jobs/metadata/view') return response(discovered ? view('generation-discovered') : late);
    if (route === '/api/jobs/metadata/pins') {
      const body = JSON.parse(String(init?.body));
      return response({ version: 1, context: { ...context, scope: 'all', view_generation: discovered ? 'generation-discovered' : context.view_generation }, results: body.selections.map((selection: unknown) => ({ selection, result: { kind: 'unavailable', reason: 'fixture' } })) });
    }
    if (route === '/api/jobs/metadata') {
      const result = list();
      if (!discovered) return response({ ...result, mode: url.searchParams.get('mode'), rows: [],
        page: { outcome: 'unavailable', revision: 0, next_cursor: null, scanned: 0, checkpoint: 0 },
        missing: [...result.missing, 'store_unavailable', 'sources_not_refreshed'],
        context: { ...result.context, scope: 'all' } });
      return response({ ...result, mode: url.searchParams.get('mode'), rows: result.rows.map(row => ({ ...row, lifecycle: url.searchParams.get('status') ?? 'running' })), context: { ...result.context, scope: 'all', view_generation: 'generation-discovered' } });
    }
    throw new Error(`Unexpected ${method} ${route}`);
  }));
  render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Observed /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(calls.filter(call => call.path === '/api/jobs/metadata/view/refresh')).toHaveLength(1);
  expect(screen.getByRole('status').textContent).toContain('job_1');
});
