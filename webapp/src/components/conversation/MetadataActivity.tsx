import { nativeActiveStatuses } from '../../lib/localJobMetadata';
import { useEffect, useId, useRef, useState } from 'react';
import { Dialog } from '@base-ui/react/dialog';
import type { Job } from '../../lib/api';
import { useSharedJobMetadata } from '../../lib/jobMetadataContext';
import { MetadataInspection } from '../MetadataJobs';
import { Modal } from '../ui/overlay';

export default function MetadataActivity({ jobs }: { jobs: readonly Job[] }) {
  const { state } = useSharedJobMetadata();
  return <ActivitySelection key={state.contextEpoch} jobs={jobs} />;
}
function ActivitySelection({ jobs }: { jobs: readonly Job[] }) {
  const { store } = useSharedJobMetadata();
  const [selected, setSelected] = useState<Job | null>(null);
  const inspectionId = useId();
  const titleId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const section = useRef<HTMLElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  useEffect(() => {
    if (!selected) return;
    const contextEpoch = store.getSnapshot().contextEpoch;
    return () => {
      if (store.getSnapshot().contextEpoch === contextEpoch) store.select(null);
    };
  }, [selected, store]);
  const active = jobs.filter(j => nativeActiveStatuses.includes(j.status) && j.read_status !== 'unavailable');
  const visible = [...active, ...jobs.filter(j => !active.includes(j))].slice(0, 8);
  const close = () => {
    returnTo.current = rowButtons.current.get(selected?.metadata_key ?? '') ?? section.current;
    setSelected(null);
    store.select(null);
  };
  return <section ref={section} tabIndex={-1} className="min-w-0 text-xs text-muted" aria-label="Observed job activity">
    {!!jobs.length && <p className="px-2 py-1">{active.length ? `At least ${active.length} active jobs` : 'No active jobs in this observation'} · Coverage incomplete</p>}
    {visible.map(job => <div key={job.metadata_key}>
      <button ref={element => { const key = job.metadata_key ?? ''; if (element) rowButtons.current.set(key, element); else rowButtons.current.delete(key); }} type="button" className="min-h-11 w-full break-words px-2 text-left hover:text-txt focus-visible:outline focus-visible:outline-accent" aria-haspopup="dialog" aria-expanded={selected?.metadata_key === job.metadata_key} aria-controls={selected?.metadata_key === job.metadata_key ? inspectionId : undefined}
        onClick={() => { if (selected?.metadata_key === job.metadata_key) close(); else { store.select(null); setSelected(job); } }}>{job.goal} · {job.status}{job.read_status === 'unavailable' ? ' · stale' : ''}</button>
    </div>)}
    {jobs.length > visible.length && <p className="px-2">Showing {visible.length} of {jobs.length} observed jobs. More in the Jobs panel.</p>}
    <Modal
      open={selected !== null}
      onClose={close}
      labelledBy={titleId}
      initialFocus={closeButton}
      finalFocus={returnTo}
      backdropClassName="fixed inset-0 z-50 bg-bg/90"
      className="fixed inset-4 z-50 m-auto h-3/4 max-h-full w-auto max-w-3xl overflow-hidden rounded-2xl border border-edge bg-panel p-0 text-txt shadow-lg outline-none"
    >
      {selected && <section id={inspectionId} aria-label="Selected job inspection" className="flex h-full min-w-0 flex-col">
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-2">
          <Dialog.Title id={titleId} className="text-sm font-semibold">Selected job inspection</Dialog.Title>
          <button ref={closeButton} type="button" className="min-h-11 shrink-0 px-2 text-xs text-muted hover:text-txt focus-visible:outline focus-visible:outline-accent" onClick={close}>Close selected inspection</button>
        </header>
        <Dialog.Description className="sr-only">Inspect the selected job without leaving the conversation.</Dialog.Description>
        <div className="min-h-0 min-w-0 overflow-y-auto overscroll-contain break-words p-4">
          <MetadataInspection job={jobs.find(job => job.metadata_key === selected.metadata_key) ?? selected} />
        </div>
      </section>}
    </Modal>
  </section>;
}
