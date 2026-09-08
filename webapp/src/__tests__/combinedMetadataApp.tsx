import type { SwarmResultsResponse } from '../lib/api';
import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import App from '../App';
import '../index.css';
import { CombinedMetadataFixture } from './metadataMigration.fixtures';

/** Local-only combined app journey. Every backend request terminates in this fixture. */
export const fixture = new CombinedMetadataFixture();
fixture.total = 1101;
export const appScenario: { display: unknown[]; resultBatches: SwarmResultsResponse['results'][]; calls: string[] } = { display: [], resultBatches: [], calls: [] };
const metadataRequest = fixture.request.bind(fixture);
const appRequest = async (method: string, path: string, body?: unknown) => {
  appScenario.calls.push(path);
  const url = new URL(path, 'http://fixture'), route = url.pathname;
  if (route.startsWith('/api/jobs/metadata') || route === '/api/endpoint' || route === '/api/jobs' || route === '/api/swarm/live' || route === '/api/jobs/artifacts/v1' || route === '/api/jobs/evidence') return metadataRequest(method, path, body);
  const sid = fixture.target.session_id, repo = fixture.target.repo;
  const sessions = [{ id: sid, title: 'Metadata fixture', active: true, repo, created_at: 1, updated_at: 2 }];
  if (method !== 'GET') return fixture.response({ ok: false, error: 'Execution is disabled in this read-only fixture.' });
  switch (route) {
    case '/api/config': return fixture.response({ driver: 'Fixture', reach: 'local', budget: 1, repo, models: [], pilot_ready: true, workers_ready: true });
    case '/api/providers': return fixture.response([{ id: 'fixture', has_key: true, models: [] }]);
    case '/api/workspace': return fixture.response({ repo, branch: 'dev', is_git: true, head_unborn: false, home: '/home', recents: [repo, '/browse-only'], codegraph_status: 'ready' });
    case '/api/workspaces': return fixture.response([{ name: 'dev', active: true, dirty: false }]);
    case '/api/sessions': case '/api/sessions/bank': return fixture.response(sessions);
    case '/api/sessions/transcript': return fixture.response({ history: [], display: appScenario.display, job_ids: ['job_1'] });
    case '/api/session/state': return fixture.response({ active_session_id: sid, session_id: sid, pending_swarms: false, resume_pending: false, runners: {}, status: 'idle' });
    case '/api/session/swarm-results': return fixture.response({ results: appScenario.resultBatches.shift() ?? [] });
    case '/api/session/queue': return fixture.response({ queue: [], recovery: [] });
    case '/api/session/goal': return fixture.response({ ok: true, goal: { text: '', token_budget: 0 } });
    case '/api/session/events': return fixture.response({ session_id: sid, events: [], cursor: 0 });
    case '/api/chat/events': return fixture.response({ session_id: sid, events: [], cursor: 0, generation: 0 });
    case '/api/mcp': return fixture.response({ servers: [], tools: [] });
    case '/api/mcp/catalog': return fixture.response({ catalog: {} });
    case '/api/diagnostics': return fixture.response({});
    case '/api/session/usage': case '/api/cost': return fixture.response({});
    default: return fixture.response([]);
  }
};
Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true, requestJSON: appRequest } });
// A stray direct fetch cannot reach a provider or the live backend from this fixture.
window.fetch = async () => { throw new Error('Direct fetch is disabled in the combined metadata fixture.'); };
localStorage.setItem('pmharness.leftOpen', '1');

function Fixture() {
  const [tick, setTick] = useState(0);
  const update = (work: () => void) => { work(); setTick(tick + 1); };
  const switchTo = (session: string, repo: string) => {
    fixture.switchTarget(session, repo);
    window.dispatchEvent(new Event('harness-config-changed'));
  };
  return <div className="h-screen flex flex-col">
    <div className="flex flex-wrap gap-2 p-2 text-xs bg-panel text-txt" aria-label="Fixture controls">
      <strong>Read-only metadata fixture: 1,101 jobs per lane</strong>
      <button onClick={() => update(() => { fixture.nativeOutcome = 'expired'; })}>Expire native pages</button>
      <button onClick={() => update(() => { fixture.nativeOutcome = 'unavailable'; })}>Make native unavailable</button>
      <button onClick={() => update(() => { fixture.nativeOutcome = 'normal'; })}>Restore native (then Retry updates)</button>
      <button onClick={() => update(() => { fixture.holdNext = true; })}>Hold next metadata request</button>
      <button onClick={() => update(() => fixture.nextRelease?.())}>Release request</button>
      <button onClick={() => update(() => switchTo('session-B', '/repo-B'))}>Switch to B</button>
      <button onClick={() => update(() => switchTo('session-A', '/repo'))}>Switch to A</button>
      <button onClick={() => update(() => window.dispatchEvent(new CustomEvent('harness-focus-tab', { detail: 'swarm' })))}>Open Jobs</button>
      <span>{fixture.calls.length} metadata requests; maximum {fixture.maximumActive} held together</span>
    </div>
    <div className="flex-1 min-h-0"><App /></div>
  </div>;
}
const root = document.getElementById('root');
if (!root) throw new Error('Fixture root missing');
export const appRoot = createRoot(root);
appRoot.render(<Fixture />);
