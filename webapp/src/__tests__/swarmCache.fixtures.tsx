import type { ReactNode } from 'react';
import { vi } from 'vitest';
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient } from '../lib/jobMetadata';
import type { MetadataContext } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { expertSummary } from './metadataExpert.fixtures';
import { handshake, list, view } from './jobMetadata.fixtures';

export function swarmCacheFixture(repo: string) {
  const store = new JobMetadataStore(new JobMetadataClient(10000));
  let context: MetadataContext = { repo, session_id: 'A', scope: 'all', view_generation: 'generation-A' };
  let goal = 'Initial A';
  const readView = vi.fn(async (captured: MetadataContext): Promise<unknown> => ({ ...view(), context: { repo: captured.repo, session_id: captured.session_id, view_generation: captured.view_generation } }));
  const fetch = vi.fn(async (path: string | URL | Request) => {
    const url = new URL(String(path), 'http://fixture');
    if (url.pathname === '/api/endpoint') return Response.json(handshake);
    if (url.pathname.endsWith('/view')) return Response.json(await readView({ ...context }));
    if (url.pathname === '/api/jobs/metadata') {
      const row = expertSummary({ repo: context.repo, session_id: context.session_id, source: 'harness', job_ref: { job_id: 'job_warm', state_id: 'store-A' } }, goal);
      const status = url.searchParams.get('status');
      return Response.json({ ...list(!status || status === row.lifecycle ? [row] : []), context,
        mode: url.searchParams.get('mode') });
    }
    throw Error(`Unexpected cache fixture request: ${url.pathname}`);
  });
  const pending = new Set<() => void>();
  const previous = Object.getOwnPropertyDescriptor(window, 'harnessIPC');
  Reflect.deleteProperty(window, 'harnessIPC');
  vi.stubGlobal('fetch', fetch);
  store.setTarget(context);
  return { store, fetch, readView,
    hangView() {
      readView.mockImplementation(captured => new Promise(resolve => {
        pending.add(() => resolve({ ...view(), context: { repo: captured.repo, session_id: captured.session_id, view_generation: captured.view_generation } }));
      }));
    },
    async settle() { for (const finish of pending) finish(); await new Promise(resolve => setTimeout(resolve, 0)); },
    target(repo: string, session_id: string, nextGoal: string) {
      context = { repo, session_id, scope: 'all', view_generation: `generation-${session_id}` };
      goal = nextGoal;
      store.setTarget(context);
    },
    Provider({ children }: { children: ReactNode }) { return <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>; },
    dispose() { store.dispose(); if (previous) Object.defineProperty(window, 'harnessIPC', previous); vi.unstubAllGlobals(); },
  };
}
