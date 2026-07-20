import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import {
  api,
  type ScheduleInfo,
  type ScheduleRun,
} from "../lib/api";

/**
 * Thin Settings schedules control: list / enable / disable / run-now / history.
 * Polls GET on open and after mutations. No SSE.
 */
export default function SchedulesPane() {
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([]);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, ScheduleRun[]>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError("");
      const data = await api.getSchedules();
      setSchedules(data.schedules || []);
    } catch (err: any) {
      setError(err?.error || "Failed to load schedules");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const flash = (msg: string) => {
    setStatus(msg);
    setTimeout(() => setStatus(""), 2500);
  };

  const mutate = async (id: string, fn: () => Promise<unknown>, okMsg: string) => {
    try {
      setBusyId(id);
      setError("");
      await fn();
      await load();
      flash(okMsg);
    } catch (err: any) {
      setError(err?.error || "Schedule action failed");
    } finally {
      setBusyId(null);
    }
  };

  const toggleHistory = async (id: string) => {
    if (expanded === id) {
      setExpanded(null);
      return;
    }
    setExpanded(id);
    try {
      setError("");
      const data = await api.getScheduleHistory(id, 20);
      setHistory((prev) => ({ ...prev, [id]: data.runs || [] }));
    } catch (err: any) {
      setError(err?.error || "Failed to load history");
    }
  };

  // IANA per-schedule zones are deferred; always host-local.
  const zoneLabel = () => "host-local";

  return (
    <div className="space-y-2">
      {error && <div className="text-risk text-[10px] font-medium">{error}</div>}
      {status && <div className="text-good text-[10px] font-medium">{status}</div>}
      <div className="text-[10px] text-muted leading-snug">
        Cron fire still needs the local schedule daemon. This panel polls; SSE is deferred.
      </div>
      <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
        {schedules.length === 0 ? (
          <div className="text-muted text-[10px]">No schedules configured.</div>
        ) : (
          schedules.map((s) => (
            <div
              key={s.id}
              className="flex flex-col p-1.5 bg-panel2/65 border border-edge/30 rounded text-[11px]"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-txt font-medium truncate">{s.name}</div>
                  <div className="text-muted font-mono text-[10px] truncate">
                    {s.cron} · {zoneLabel()} · {s.display_status || s.last_status || "never"}
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <input
                    type="checkbox"
                    checked={!!s.enabled}
                    disabled={busyId === s.id}
                    onChange={() =>
                      mutate(
                        s.id,
                        () =>
                          s.enabled
                            ? api.disableSchedule(s.id)
                            : api.enableSchedule(s.id),
                        s.enabled ? "Disabled" : "Enabled",
                      )
                    }
                    className="rounded border-edge text-accent focus:ring-accent bg-panel2"
                    title="Enable / disable"
                  />
                  <button
                    type="button"
                    disabled={busyId === s.id}
                    onClick={() =>
                      mutate(
                        s.id,
                        () => api.runScheduleNow(s.id),
                        "Run started",
                      )
                    }
                    className="text-[10px] px-1.5 py-0.5 rounded border border-edge text-muted hover:text-txt hover:border-accent/40 disabled:opacity-50"
                    title="Run now"
                  >
                    Run
                  </button>
                  <button
                    type="button"
                    onClick={() => toggleHistory(s.id)}
                    className="text-muted hover:text-txt p-0.5"
                    title="History"
                  >
                    {expanded === s.id ? (
                      <ChevronDown size={12} />
                    ) : (
                      <ChevronRight size={12} />
                    )}
                  </button>
                </div>
              </div>
              {s.next_fires && s.next_fires.length > 0 && (
                <div className="text-faint text-[9px] mt-1 font-mono">
                  next: {s.next_fires.slice(0, 2).join(", ")}
                </div>
              )}
              {expanded === s.id && (
                <div className="mt-1.5 border-t border-edge/20 pt-1.5 space-y-0.5 max-h-24 overflow-y-auto">
                  {(history[s.id] || []).length === 0 ? (
                    <div className="text-muted text-[10px]">No runs yet.</div>
                  ) : (
                    (history[s.id] || []).map((r) => (
                      <div
                        key={r.id}
                        className="text-[10px] font-mono text-muted truncate"
                      >
                        {r.status}
                        {r.halt_reason ? ` — ${r.halt_reason}` : ""}
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>
      <button
        type="button"
        onClick={() => load()}
        className="text-[10px] text-muted hover:text-txt underline-offset-2 hover:underline"
      >
        Refresh
      </button>
    </div>
  );
}
