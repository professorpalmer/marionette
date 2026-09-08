import { act } from '@testing-library/react';
import type { ReactNode } from 'react';
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient } from '../lib/jobMetadata';
import type { MetadataContext, MetadataSelection, MetadataSummary } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { context, detail, handshake, list, summary, view } from './jobMetadata.fixtures';

/** Exercise the public metadata parser and store; only the transport is a fixture. */
export async function navigationFixture(jobIds: string[], owner = { repo: '/repo', session_id: 'session-A' }) {
  let target: MetadataContext = { ...context, ...owner, scope: 'all' };
  let ids = jobIds;
  const previousBridge = Object.getOwnPropertyDescriptor(window, 'harnessIPC');
  const selectionFor = (jobId: string): MetadataSelection => ({
    repo: target.repo, session_id: target.session_id, source: 'harness',
    job_ref: { job_id: jobId, state_id: 'store-A' },
  });
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: {
    endpointHeaders: true,
    requestJSON: async (_method: string, path: string) => {
      const url = new URL(path, 'http://fixture');
      let body: unknown;
      if (url.pathname === '/api/endpoint') body = handshake;
      else if (url.pathname.endsWith('/view')) body = { ...view(), context: { repo: target.repo, session_id: target.session_id, view_generation: target.view_generation } };
      else if (url.pathname.endsWith('/pins')) body = { version: 1, context: target, results: [] };
      else if (url.pathname.endsWith('/detail')) body = { ...detail(), context: target, selection: selectionFor(url.searchParams.get('job_id') || ids[0]) };
      else if (url.pathname === '/api/jobs/metadata') {
        const rows: MetadataSummary[] = ids.map(id => ({ ...summary(), selection: selectionFor(id), ownership: { ...summary().ownership, session_id: target.session_id } }));
        const status = url.searchParams.get('status');
        body = { ...list(rows.filter(row => !status || status === row.lifecycle)), context: target, mode: url.searchParams.get('mode') };
      } else throw Error(`Unexpected navigation fixture request: ${path}`);
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(body) };
    },
  } });
  const store = new JobMetadataStore(new JobMetadataClient(1000));
  async function observe() {
    await act(async () => { store.setTarget(target); await store.readView(); });
    for (let i = 0; i < 48; i++) await act(async () => { await store.advance(); });
    if (store.getSnapshot().observations.length !== ids.length) throw Error(`Navigation fixture did not observe its jobs: ${store.getSnapshot().error}`);
  }
  function dispose() {
    store.dispose();
    if (previousBridge) Object.defineProperty(window, 'harnessIPC', previousBridge);
    else Reflect.deleteProperty(window, 'harnessIPC');
  }
  try { await observe(); } catch (error) { dispose(); throw error; }
  return {
    store,
    selection: selectionFor,
    context: () => ({ repo: target.repo, session_id: target.session_id, scope: target.scope, contextEpoch: store.getSnapshot().contextEpoch }),
    async switchOwner(owner: { repo: string; session_id: string }, jobIds = ids) {
      target = { ...target, ...owner }; ids = jobIds; await observe();
    },
    Provider({ children }: { children: ReactNode }) {
      return <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>;
    },
    dispose,
  };
}
