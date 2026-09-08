import { act } from '@testing-library/react';
import type { ReactNode } from 'react';
import { vi } from 'vitest';
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { context, handshake, list, selection, view } from './jobMetadata.fixtures';
import { expertSummary } from './metadataExpert.fixtures';
import nativeWire from './nativeSelectedContext.backend.json';

export async function commandSplitFixture(goals: string[], kind: LocalSummary['kind']) {
  const rows = goals.map((goal, index) => expertSummary({ ...selection(index + 1), job_ref: {
    ...selection(index + 1).job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc',
  } }, goal));
  const labels = { run_command: 'Command', run_command_batch: 'Command batch', parallel_wave: 'Parallel wave', provider: 'Provider worker' };
  const native: LocalSummary = { ...nativeWire.detail.summary, lifecycle: 'running', kind,
    display: { label: labels[kind], model: '', adapter: kind, truncated: false } };
  const previousBridge = Object.getOwnPropertyDescriptor(window, 'harnessIPC');
  const request = vi.fn(async (_method: string, path: string) => {
    const url = new URL(path, 'http://fixture');
    let value: unknown;
    if (url.pathname === '/api/endpoint') value = handshake;
    else if (url.pathname.endsWith('/view')) value = { ...view(), local: nativeWire.descriptor };
    else if (url.pathname.endsWith('/local')) {
      const active = url.searchParams.get('lane') === 'active';
      value = { ...(active ? nativeWire.active : nativeWire.history), rows: [native] };
    } else if (url.pathname === '/api/jobs/metadata') {
      const status = url.searchParams.get('status');
      value = { ...list(rows.filter(row => !status || row.lifecycle === status)), mode: url.searchParams.get('mode') };
    } else throw Error(`Unexpected metadata request: ${url.pathname}`);
    return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(value) };
  });
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true, requestJSON: request } });
  const store = new JobMetadataStore(new JobMetadataClient(1000));
  await act(async () => {
    store.setTarget(context);
    if (await store.readView() !== 'applied') throw Error('Metadata view rejected');
    for (let i = 0; i < 16; i++) {
      if (store.getSnapshot().observations.length === rows.length && store.getSnapshot().local.observations.length === 1) break;
      await store.advance();
    }
  });
  if (store.getSnapshot().observations.length !== rows.length || store.getSnapshot().local.observations.length !== 1)
    throw Error('Bounded fixture observations rejected');
  return { store, request,
    Provider({ children }: { children: ReactNode }) { return <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>; },
    dispose() { store.dispose(); if (previousBridge) Object.defineProperty(window, 'harnessIPC', previousBridge); else Reflect.deleteProperty(window, 'harnessIPC'); },
  };
}
