import { useState } from "react";
import type { ScheduleInfo, ScheduleWrite } from "../lib/api";

export const scheduleButton = "min-h-11 rounded border border-edge px-3 py-2 text-sm text-txt hover:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50";
const inputClass = "w-full min-h-11 rounded border border-edge bg-panel2 px-3 py-2 text-sm text-txt focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent";

export default function ScheduleEditor({ initial, pending, onSave, onCancel }: {
  initial: ScheduleInfo | null;
  pending: boolean;
  onSave: (value: ScheduleWrite) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<ScheduleWrite>(() => ({
    request_id: initial ? undefined : crypto.randomUUID(),
    name: initial?.name ?? "", objective: initial?.objective ?? "",
    cron: initial?.cron ?? "0 9 * * *", repo: initial?.repo ?? "",
    interval_seconds: initial?.interval_seconds ?? 0,
    timezone: initial?.timezone ?? "UTC", enabled: initial?.enabled ?? true,
    driver: initial?.driver ?? "", swarm_adapter: initial?.swarm_adapter ?? "agentic",
    max_tokens: initial?.max_tokens ?? 0, max_seconds: initial?.max_seconds ?? 0,
    max_swarms: initial?.max_swarms ?? 0, missed_policy: initial?.missed_policy ?? "once",
    notepad: initial?.notepad ?? "", monitor_mode: initial?.monitor_mode ?? false,
    failure_deliver: initial?.failure_deliver ?? "route",
  }));
  return <form aria-label={initial ? "Edit schedule" : "New schedule"} className="space-y-3 rounded border border-edge bg-panel p-3"
    onSubmit={event => { event.preventDefault(); if (!pending) onSave(draft); }}>
    <h3 className="text-sm font-medium">{initial ? `Edit ${initial.name}` : "New schedule"}</h3>
    <fieldset disabled={pending} className="space-y-3">
      <label className="block text-sm">Name<input autoFocus required className={inputClass} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
      <label className="block text-sm">Objective<textarea required className={inputClass} rows={3} value={draft.objective} onChange={e => setDraft({ ...draft, objective: e.target.value })} /></label>
      <label className="block text-sm">Project path<input required className={inputClass} placeholder="Absolute project path" value={draft.repo} onChange={e => setDraft({ ...draft, repo: e.target.value })} /></label>
      <p className="text-sm text-muted">{initial?.delivery_mode ? `Existing delivery mode ${initial.delivery_mode} is preserved; its target is configured by the daemon integration.` : "Each run starts a fresh session in this saved project. Switching projects or chats does not change it."}</p>
      <label className="block text-sm">Driver<input required={!initial} className={inputClass} placeholder="provider:model" value={draft.driver} onChange={e => setDraft({ ...draft, driver: e.target.value })} /></label>
      <label className="block text-sm">Timing<select className={inputClass} value={draft.interval_seconds ? "interval" : "cron"} onChange={e => setDraft({ ...draft, interval_seconds: e.target.value === "interval" ? 300 : 0, cron: e.target.value === "interval" ? "" : "0 9 * * *", missed_policy: "once" })}><option value="cron">Cron</option><option value="interval">Elapsed interval</option></select></label>
      {draft.interval_seconds ? <label className="block text-sm">Every (minutes)<input required type="number" min={1} max={525600} step={1} className={inputClass} value={draft.interval_seconds / 60} onChange={e => setDraft({ ...draft, interval_seconds: e.target.valueAsNumber * 60 })} /></label> : <label className="block text-sm">Cron expression<input required className={`${inputClass} font-mono`} aria-describedby="schedule-cron-help" value={draft.cron} onChange={e => setDraft({ ...draft, cron: e.target.value })} /></label>}
      <p id="schedule-cron-help" className="text-sm text-muted">Minute hour day-of-month month day-of-week. Example: 0 9 * * 1-5 runs weekdays at 09:00.</p>
      <label className="block text-sm">Timezone<input className={inputClass} aria-describedby="schedule-zone-help" placeholder="America/Chicago" value={draft.timezone} onChange={e => setDraft({ ...draft, timezone: e.target.value })} /></label>
      <p id="schedule-zone-help" className="text-sm text-muted">Use an IANA name such as America/Chicago or UTC. Empty uses the daemon host’s local timezone. Spring gaps are skipped; repeated fall minutes run once at the first occurrence.</p>
      <label className="block text-sm">Missed runs<select disabled={!!draft.interval_seconds} className={inputClass} value={draft.missed_policy} onChange={e => setDraft({ ...draft, missed_policy: e.target.value })}>
        <option value="once">Run latest missed occurrence once</option><option value="skip">Skip missed occurrences</option><option value="all">Catch up all (up to 100 per tick)</option>
      </select></label>
      {!initial && <label className="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />Enable schedule after saving</label>}
      <details><summary className="min-h-11 cursor-pointer py-2 text-sm focus-visible:outline focus-visible:outline-accent">Run limits and continuity</summary>
        <div className="space-y-3">
          <p className="text-sm text-muted">Zero uses the governor default. Runs use the saved driver and project. Intervals run the latest missed occurrence once.</p>
          {(["max_tokens", "max_seconds", "max_swarms"] satisfies (keyof ScheduleWrite)[]).map(key => <label key={key} className="block text-sm">
            {key === "max_tokens" ? "Maximum tokens" : key === "max_seconds" ? "Maximum seconds" : "Maximum swarms"}
            <input className={inputClass} type="number" min={0} step={1} required value={draft[key]} onChange={e => setDraft({ ...draft, [key]: e.target.valueAsNumber })} />
          </label>)}
          <label className="block text-sm">Swarm adapter<input required className={inputClass} value={draft.swarm_adapter} onChange={e => setDraft({ ...draft, swarm_adapter: e.target.value })} /></label>
          <label className="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={draft.monitor_mode} onChange={e => setDraft({ ...draft, monitor_mode: e.target.checked })} />Monitor with continuity</label>
          <label className="block text-sm">Notepad<textarea className={inputClass} maxLength={4096} value={draft.notepad} onChange={e => setDraft({ ...draft, notepad: e.target.value })} /></label>
          <label className="block text-sm">Failure notifications<select className={inputClass} value={draft.failure_deliver} onChange={e => setDraft({ ...draft, failure_deliver: e.target.value })}><option value="route">Deliver to configured route</option><option value="suppress">Suppress failures</option></select></label>
        </div>
      </details>
    </fieldset>
    <div className="flex flex-wrap gap-2">
      <button type="submit" disabled={pending} className={scheduleButton}>{pending ? "Saving schedule…" : "Save schedule"}</button>
      <button type="button" disabled={pending} onClick={onCancel} className={scheduleButton}>Cancel</button>
    </div>
  </form>;
}
