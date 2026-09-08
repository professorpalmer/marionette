import { nativeActiveStatuses } from '../../lib/localJobMetadata';
import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Job } from '../../lib/api';
import { useSharedJobMetadata } from '../../lib/jobMetadataContext';
import { MetadataInspection } from '../MetadataJobs';

export default function MetadataActivity({ jobs }: { jobs: readonly Job[] }) {
  const { state } = useSharedJobMetadata();
  return <ActivitySelection key={state.contextEpoch} jobs={jobs} />;
}
function ActivitySelection({ jobs }: { jobs: readonly Job[] }) {
  const { store } = useSharedJobMetadata();
  const [selected, setSelected] = useState<Job | null>(null);
  const inspectionId = useId();
  const titleId = useId();
  const descriptionId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const section = useRef<HTMLElement>(null);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  useEffect(() => {
    if (!selected) return;
    const contextEpoch = store.getSnapshot().contextEpoch;
    const surface = dialog.current;
    if (surface) {
      if (surface.showModal) surface.showModal();
      else surface.open = true;
    }
    closeButton.current?.focus();
    return () => {
      surface?.close?.();
      if (store.getSnapshot().contextEpoch === contextEpoch) store.select(null);
    };
  }, [selected, store]);
  const active = jobs.filter(j => nativeActiveStatuses.includes(j.status) && j.read_status !== 'unavailable');
  const visible = [...active, ...jobs.filter(j => !active.includes(j))].slice(0, 8);
  const close = () => {
    const returnTo = rowButtons.current.get(selected?.metadata_key ?? '') ?? section.current;
    dialog.current?.close?.();
    setSelected(null);
    store.select(null);
    returnTo?.focus();
  };
  return <section ref={section} tabIndex={-1} className="min-w-0 text-xs text-muted" aria-label="Observed job activity">
    {!!jobs.length && <p className="px-2 py-1">{active.length ? `At least ${active.length} active jobs` : 'No active jobs in this observation'} · Coverage incomplete</p>}
    {visible.map(job => <div key={job.metadata_key}>
      <button ref={element => { const key = job.metadata_key ?? ''; if (element) rowButtons.current.set(key, element); else rowButtons.current.delete(key); }} type="button" className="min-h-11 w-full break-words px-2 text-left hover:text-txt focus-visible:outline focus-visible:outline-accent" aria-haspopup="dialog" aria-expanded={selected?.metadata_key === job.metadata_key} aria-controls={selected?.metadata_key === job.metadata_key ? inspectionId : undefined}
        onClick={() => { if (selected?.metadata_key === job.metadata_key) close(); else { store.select(null); setSelected(job); } }}>{job.goal} · {job.status}{job.read_status === 'unavailable' ? ' · stale' : ''}</button>
    </div>)}
    {jobs.length > visible.length && <p className="px-2">Showing {visible.length} of {jobs.length} observed jobs. More in the Jobs panel.</p>}
    {selected && createPortal(<dialog ref={dialog} aria-labelledby={titleId} aria-describedby={descriptionId}
      className="fixed inset-4 m-auto h-3/4 max-h-full w-auto max-w-3xl overflow-hidden rounded-2xl border border-edge bg-panel p-0 text-txt shadow-lg backdrop:bg-bg/90"
      onCancel={event => { event.preventDefault(); close(); }}
      onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); } }}>
      <section id={inspectionId} aria-label="Selected job inspection" className="flex h-full min-w-0 flex-col">
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-2">
          <h2 id={titleId} className="text-sm font-semibold">Selected job inspection</h2>
          <button ref={closeButton} type="button" className="min-h-11 shrink-0 px-2 text-xs text-muted hover:text-txt focus-visible:outline focus-visible:outline-accent" onClick={close}>Close selected inspection</button>
        </header>
        <p id={descriptionId} className="sr-only">Inspect the selected job without leaving the conversation.</p>
        <div className="min-h-0 min-w-0 overflow-y-auto overscroll-contain break-words p-4">
          <MetadataInspection job={jobs.find(job => job.metadata_key === selected.metadata_key) ?? selected} />
        </div>
      </section>
    </dialog>, document.body)}
  </section>;
}
