import { peekTranscriptCacheEntry } from '../components/conversation/transcriptCache';
import { act, waitFor, screen, fireEvent } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(window, 'harnessIPC'); });
it('boots the actual combined App fixture without any legacy job body request', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
  document.body.innerHTML = '<div id="root"></div>';
  const fixtureModule = await import('./combinedMetadataApp');
  fixtureModule.fixture.total = 1;
  fixtureModule.fixture.pmLifecycle = 'completed';
  fixtureModule.appScenario.display = [{ type: 'swarm_pending', job_ids: ['job_1'], objective: 'Await exact result delivery', status: 'running' }];
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)); });
  await waitFor(() => expect(fixtureModule.fixture.calls.some(c => c.path === '/api/jobs/metadata/view')).toBe(true), { timeout: 8000 });
  expect(fixtureModule.fixture.calls.some(c => /^\/api\/(jobs|swarm\/live)(\?|$)/.test(c.path))).toBe(false);
  await waitFor(() => expect(fixtureModule.appScenario.calls.filter(p => p === '/api/session/swarm-results').length).toBeGreaterThanOrEqual(5), { timeout: 12000 });
  expect(peekTranscriptCacheEntry('session-A')?.items.some(item => item.kind === 'swarm_pending' && item.status === 'running')).toBe(true);
  fixtureModule.appScenario.resultBatches = [[], [
    { kind: 'swarm_result', data: { job_id: 'job_1', applied: false, files: [], summary: 'Recovered exact result', error: 'Fixture failure' } },
    { kind: 'swarm_result', data: { job_id: 'job_other', applied: false, files: [], summary: 'Unrelated result also delivered', error: 'Other fixture failure' } },
  ]];
  await waitFor(() => expect(fixtureModule.appScenario.resultBatches).toHaveLength(0), { timeout: 6000 });
  for (const fold of screen.queryAllByTestId('activity-fold')) {
    const button = fold.querySelector('button'); if (button) fireEvent.click(button);
  }
  for (const card of screen.getAllByTestId('swarm-result-card')) { const button = card.querySelector('button'); if (button) fireEvent.click(button); }
  expect(screen.getByText('Recovered exact result')).toBeInTheDocument();
  expect(screen.getByText('Unrelated result also delivered')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Open Jobs'));
  await waitFor(() => expect(screen.getByRole('region', { name: 'Jobs' })).toBeInTheDocument());
  expect(fixtureModule.fixture.calls.some(c => /^\/api\/(jobs|swarm\/live)(\?|$)/.test(c.path))).toBe(false);
  act(() => fixtureModule.appRoot.unmount());
}, 30000);
