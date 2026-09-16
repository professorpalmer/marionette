import { useEffect, useRef, useState } from "react";
import { getSessionCache, updateSessionCache, type CacheCommand, type SessionCache } from "../../lib/sessionCache";

export default function SessionCacheControl({ sessionId, busy }: { sessionId: string; busy: boolean }) {
  const [cache, setCache] = useState<SessionCache | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const owner = useRef(sessionId);
  owner.current = sessionId;
  const revision = useRef(0);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const requestRevision = ++revision.current;
      try {
        const result = await getSessionCache(sessionId);
        if (active && revision.current === requestRevision) setCache(result);
      } catch {
        if (active && revision.current === requestRevision) setError("Cache controls are unavailable for this session.");
      }
    };
    setCache(null);
    setError("");
    setSaving(false);
    dialog.current?.close();
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    window.addEventListener("harness-cache-updated", refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("harness-cache-updated", refresh); };
  }, [sessionId]);

  async function update(command: CacheCommand) {
    const capturedOwner = sessionId;
    ++revision.current;
    setSaving(true);
    setError("");
    try {
      const result = await updateSessionCache(capturedOwner, command);
      if (owner.current === capturedOwner) setCache(result);
    } catch {
      if (owner.current === capturedOwner) setError("Could not update cache controls. Finish the turn and try again.");
    } finally {
      if (owner.current === capturedOwner) setSaving(false);
    }
  }

  const current = cache?.session_id === sessionId ? cache : null;
  return <>
    <button type="button" className="text-[10px] text-muted hover:text-txt shrink-0"
      title={current?.reason || error || "Session cache controls"} onClick={() => dialog.current?.showModal()}>
      Cache: {current?.state || "unavailable"}
    </button>
    <dialog ref={dialog} className="m-auto w-96 max-w-[90vw] rounded-lg border border-edge bg-panel p-4 text-txt shadow-xl backdrop:bg-black/40">
      <div className="flex items-center justify-between mb-3">
        <strong className="text-sm">Session cache</strong>
        <button type="button" className="text-xs text-muted hover:text-txt" onClick={() => dialog.current?.close()}>Close</button>
      </div>
      <p className="text-xs text-muted mb-3">{current?.reason || error}</p>
      <p className="text-xs text-muted mb-3">Keep warm allows up to 3 refreshes, 1 hour idle, and a $5 input/output reserve. Refreshes bill cached reads or writes as well as output. It stops when this runner closes.</p>
      <button type="button" disabled={saving || !current} className="text-xs rounded border border-edge px-3 py-1.5 disabled:opacity-50"
        onClick={() => void update({ action: current?.enabled ? "stop" : "start" })}>
        {current?.enabled ? "Stop keep warm" : "Start keep warm"}
      </button>
      {current ? <p className="text-xs text-faint mt-2">{current.refreshes}/{current.max_refreshes} refreshes; ${current.reserved_usd.toFixed(2)} reserved (not measured spend).</p> : null}
      <label className="flex items-center gap-2 text-xs mt-4">
        <input type="checkbox" checked={current?.retain_reasoning || false} disabled={saving || busy || !current}
          onChange={event => void update({ action: "preferences", retain_reasoning: event.target.checked })} />
        Keep thinking blocks across turns
      </label>
      <p className="text-xs text-muted mt-2">Off by default. Retains signed or encrypted provider output in this session for cache reuse. Requires thinking on; replay stays with the same provider and model.</p>
      {error ? <p role="status" className="text-xs text-warn mt-3">{error}</p> : null}
    </dialog>
  </>;
}
