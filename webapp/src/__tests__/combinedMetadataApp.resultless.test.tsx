import { act } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { RESULT_RECOVERY_DRAIN_LIMIT } from '../components/conversation/swarmPoll';
import { advanceUntil, useFakePollTimers } from './fakeTime';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); Reflect.deleteProperty(window, 'harnessIPC'); });

const bodyHas = (re: RegExp) => (document.body.innerHTML.match(re) ?? []).length;

// An interrupted run_parallel worker is terminal on the backend but never
// produces a swarm_result card. The poll must stop holding Still working…
// once the bounded recovery drains ran empty, instead of looping forever.
it('settles a terminal job that never delivers a result card and stops draining', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
  document.body.innerHTML = '<div id="root"></div>';
  // The app mounts on import, so its poll intervals must start on fake time.
  useFakePollTimers();
  const fixtureModule = await import('./combinedMetadataApp');
  fixtureModule.fixture.total = 1;
  fixtureModule.fixture.pmLifecycle = 'completed';
  fixtureModule.appScenario.display = [{ type: 'swarm_pending', job_ids: ['job_1'], objective: 'Interrupted implement wave', status: 'running' }];
  fixtureModule.appScenario.resultBatches = [];
  await advanceUntil(() => bodyHas(/Still working/g) > 0, 100, 8000);
  const drains = () => fixtureModule.appScenario.calls.filter(p => p === '/api/session/swarm-results').length;
  // Each poll tick spends one results call plus one recovery drain.
  await advanceUntil(() => drains() >= 2 * RESULT_RECOVERY_DRAIN_LIMIT, 1000, 60000);
  await advanceUntil(() => bodyHas(/Still working/g) === 0, 1000, 20000);
  const settledAt = drains();
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(drains()).toBe(settledAt);
  act(() => fixtureModule.appRoot.unmount());
}, 20000); // App render under a loaded worker pool, not timers
