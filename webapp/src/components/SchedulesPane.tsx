import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ScheduleInfo, type ScheduleRun, type ScheduleWrite } from "../lib/api";
import ScheduleEditor, { scheduleButton } from "./ScheduleEditor";

type Editor = { kind: "closed" } | { kind: "create" } | { kind: "edit"; schedule: ScheduleInfo };
type History = { kind: "closed" } | { kind: "loading"; id: string } | { kind: "ready"; id: string; runs: ScheduleRun[] } | { kind: "error"; id: string; error: string };
function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "error" in error && typeof error.error === "string") return error.error;
  return "Schedule request failed. Refresh to check the saved state before retrying.";
}

export default function SchedulesPane() {
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [editor, setEditor] = useState<Editor>({ kind: "closed" });
  const [editorVersion, setEditorVersion] = useState(0);
  const [history, setHistory] = useState<History>({ kind: "closed" });
  const [pending, setPending] = useState<string | null>(null);
  const [manualRunId, setManualRunId] = useState<string | null>(null);
  const [pausingRun, setPausingRun] = useState(false);
  const pauseBusy = useRef(false);
  const busy = useRef(false);
  const mounted = useRef(false);
  const loadGeneration = useRef(0);
  const historyGeneration = useRef(0);
  const createButton = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);
  useEffect(() => {
    if (editor.kind === "closed" && pending === null && restoreFocus.current) {
      createButton.current?.focus();
      restoreFocus.current = false;
    }
  }, [editor.kind, pending]);

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    try {
      const data = await api.getSchedules();
      if (!mounted.current || generation !== loadGeneration.current) return;
      setSchedules(data.schedules);
      setLoaded(true);
    } catch (err: unknown) {
      if (mounted.current && generation === loadGeneration.current) setError(errorText(err));
    } finally {
      if (mounted.current && generation === loadGeneration.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = window.setInterval(() => { if (!busy.current) void load(); }, 3000);
    return () => { window.clearInterval(timer); mounted.current = false; loadGeneration.current++; historyGeneration.current++; };
  }, [load]);

  const mutate = async (id: string, action: () => Promise<string>) => {
    if (busy.current) return;
    busy.current = true;
    ++loadGeneration.current;
    ++historyGeneration.current;
    setHistory({ kind: "closed" });
    setLoading(false);
    setPending(id);
    setError("");
    setStatus("");
    try {
      const message = await action();
      if (mounted.current) setStatus(message);
    } catch (err: unknown) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) {
        await load();
        if (mounted.current) setPending(null);
      }
      busy.current = false;
    }
  };

  const save = (draft: ScheduleWrite) => {
    const snapshot = editor;
    if (snapshot.kind === "closed") return;
    void mutate("editor", async () => {
      if (snapshot.kind === "edit") {
        const { enabled: _enabled, request_id: _requestId, ...patch } = draft;
        await api.updateSchedule(snapshot.schedule.id, { ...patch, revision: snapshot.schedule.revision });
      }
      else await api.addSchedule(draft);
      if (mounted.current) { restoreFocus.current = true; setEditor({ kind: "closed" }); }
      return snapshot.kind === "edit" ? "Schedule updated" : "Schedule created";
    });
  };

  const pauseRunningSchedule = async (schedule: ScheduleInfo) => {
    if (pauseBusy.current) return;
    pauseBusy.current = true;
    setPausingRun(true);
    try {
      const paused = await api.disableSchedule(schedule.id, schedule.revision);
      if (mounted.current) {
        ++loadGeneration.current;
        setLoading(false);
        setSchedules(rows => rows.map(row => row.id === paused.id && (row.revision ?? 0) <= (paused.revision ?? 0) ? paused : row));
        setStatus("Schedule paused; cancellation requested");
      }
    } catch (err: unknown) {
      if (mounted.current) setError(errorText(err));
    } finally {
      pauseBusy.current = false;
      if (mounted.current) setPausingRun(false);
    }
  };

  const toggleHistory = async (id: string) => {
    const generation = ++historyGeneration.current;
    if (history.kind !== "closed" && history.id === id) { setHistory({ kind: "closed" }); return; }
    setHistory({ kind: "loading", id });
    try {
      const data = await api.getScheduleHistory(id, 20);
      if (mounted.current && generation === historyGeneration.current) setHistory({ kind: "ready", id, runs: data.runs });
    } catch (err: unknown) {
      if (mounted.current && generation === historyGeneration.current) setHistory({ kind: "error", id, error: errorText(err) });
    }
  };

  return <section aria-label="Schedules" className="space-y-3 text-txt">
    <p className="text-sm text-muted">Recurring runs fire while the app is open. An external daemon can also run these schedules. Pause requests cancellation and keeps the active claim until execution exits.</p>
    <div className="flex flex-wrap gap-2">
      <button ref={createButton} type="button" disabled={pending !== null || editor.kind !== "closed"} className={scheduleButton} onClick={() => { setError(""); setEditor({ kind: "create" }); }}>Create schedule</button>
      <button type="button" disabled={pending !== null || loading} className={scheduleButton} onClick={() => { setError(""); void load(); }}>Refresh schedules</button>
    </div>
    {error && <p role="alert" className="text-sm text-risk">{error}</p>}
    {status && <p role="status" className="text-sm text-good">{status}</p>}
    {editor.kind !== "closed" && <ScheduleEditor key={editor.kind === "edit" ? `${editor.schedule.id}:${editorVersion}` : "new"} initial={editor.kind === "edit" ? editor.schedule : null} pending={pending !== null} onSave={save} onCancel={() => { restoreFocus.current = true; setEditor({ kind: "closed" }); setError(""); }} />}
    {editor.kind === "edit" && <button type="button" className={scheduleButton} disabled={pending !== null || loading} onClick={() => {
      const latest = schedules.find(s => s.id === editor.schedule.id);
      if (!latest) { setError("Schedule no longer exists. Cancel this edit and create a new schedule."); return; }
      setEditor({ kind: "edit", schedule: latest });
      setEditorVersion(version => version + 1);
      setError("");
    }}>Reload saved values</button>}
    {loading && <p role="status" className="text-sm text-muted">Loading schedules…</p>}
    {loaded && schedules.length === 0 && <p className="text-sm text-muted">No schedules configured. Create one to run an objective on a recurring schedule.</p>}
    <div className="space-y-3">
      {schedules.map(s => {
        const running = manualRunId === s.id || s.display_status === "running";
        const cancelOnly = running && !s.enabled;
        return <article key={s.id} className="space-y-2 rounded border border-edge bg-panel2 p-3" aria-label={s.name} aria-busy={pending === s.id}>
        <h3 className="text-sm font-medium break-words">{s.name}</h3>
        <p className="text-sm text-muted break-words">{s.objective}</p>
        <p className="text-sm font-mono break-words">{s.interval_seconds ? `Every ${s.interval_seconds / 60} minutes (elapsed)` : `${s.cron} · ${s.timezone || "Host-local (daemon timezone)"}`}</p>
        <p className="text-sm text-muted break-all">Project: {s.repo || "Not set — edit before running"} · {s.delivery_mode ? `Legacy delivery: ${s.delivery_mode} (daemon target)` : "Fresh session per run"}</p>
        <p className="text-sm text-muted">{s.enabled ? "Enabled" : "Paused"} · {s.display_status || s.last_status || "Never run"}</p>
        {s.enabled && <p className="text-sm text-muted break-words">Next ({s.timezone || "daemon host-local"}): {s.next_fires?.length ? s.next_fires.slice(0, 3).join(", ") : "Unavailable — check the cron and timezone"}</p>}
        {(s.monitor_mode || s.failure_deliver === "suppress") && <p className="text-sm text-muted">{[s.monitor_mode ? "Monitor continuity" : "", s.failure_deliver === "suppress" ? "Failure notifications suppressed" : ""].filter(Boolean).join(" · ")}</p>}
        <div className="flex flex-wrap gap-2">
          <button type="button" className={scheduleButton} disabled={pending !== null || editor.kind !== "closed"} aria-label={`Edit ${s.name}`} onClick={() => { setError(""); setEditor({ kind: "edit", schedule: s }); }}>Edit</button>
          <button type="button" className={scheduleButton} disabled={pausingRun || (pending !== null && manualRunId !== s.id)} aria-label={cancelOnly ? `Cancel run for ${s.name}` : `${s.enabled ? "Pause" : "Resume"} ${s.name}`} onClick={() => {
            if (running) { void pauseRunningSchedule(s); return; }
            void mutate(s.id, async () => {
            if (s.enabled) await api.disableSchedule(s.id, s.revision); else await api.enableSchedule(s.id, s.revision);
            return s.enabled ? "Schedule paused" : "Schedule resumed";
          }); }}>{pausingRun && manualRunId === s.id ? "Cancelling…" : cancelOnly ? "Cancel run" : s.enabled ? "Pause" : "Resume"}</button>
          <button type="button" className={scheduleButton} disabled={pending !== null || s.display_status === "running"} aria-label={`Run ${s.name} now`} onClick={() => void mutate(s.id, async () => {
            setManualRunId(s.id);
            try {
              const result = await api.runScheduleNow(s.id, s.revision);
              if (!result.ok) throw new Error(`${result.run.status}: ${result.run.halt_reason || "Open history for details"}`);
              return `Run finished: ${result.run.status}`;
            } finally {
              if (mounted.current) setManualRunId(null);
            }
          })}>Run now</button>
          <button type="button" className={scheduleButton} disabled={pending !== null} aria-expanded={history.kind !== "closed" && history.id === s.id} aria-label={`History for ${s.name}`} onClick={() => void toggleHistory(s.id)}>History</button>
        </div>
        {pending === s.id && <p role="status" className="text-sm text-muted">Waiting for schedule result…</p>}
        {history.kind !== "closed" && history.id === s.id && <div className="space-y-2 border-t border-edge pt-2">
          {history.kind === "loading" && <p role="status">Loading history…</p>}
          {history.kind === "error" && <p role="alert" className="text-risk">{history.error}</p>}
          {history.kind === "ready" && (history.runs.length === 0 ? <p className="text-sm text-muted">No runs yet.</p> : history.runs.map(r => <article key={r.id} className="space-y-1 text-sm text-muted break-words">
            <p>{r.started_at ? new Date(r.started_at * 1000).toLocaleString() : ""} · {r.status}{r.halt_reason ? ` — ${r.halt_reason}` : ""}</p>
            {r.session_id && <p>Session: {r.session_id}</p>}
            <p>{r.tokens_used ?? "Unknown"} tokens · {r.usage_receipt?.cost_usd == null ? "Cost unknown" : `$${r.usage_receipt.cost_usd.toFixed(6)} (${r.usage_receipt.source || "reported"})`}</p>
            {r.result_text && <p className="whitespace-pre-wrap text-txt">{r.result_text}</p>}
          </article>))}
        </div>}
      </article>;
      })}
    </div>
  </section>;
}
