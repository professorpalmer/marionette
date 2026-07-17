import { useEffect, useState } from "react";
import { ChevronDown, Folder, GitBranch } from "lucide-react";
import { api } from "../../lib/api";
import { pickFolder } from "../../lib/transport";
import {
  formatWorkspaceOpenLeaseExhaustedMessage,
  isWorkspaceOpenLeaseExhausted,
} from "./leaseExhausted";
import { workspaceLeafName } from "./workspaceDisplay";

export default function WorkspaceChip() {
  const [ws, setWs] = useState<{ repo: string; branch: string; recents?: string[]; home?: string } | null>(null);
  const [open, setOpen] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const refresh = () => api.getWorkspace().then((w) => setWs(w as any)).catch(() => {});
  useEffect(() => {
    refresh();
    const h = () => refresh();
    window.addEventListener("harness-config-changed", h);
    return () => window.removeEventListener("harness-config-changed", h);
  }, []);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onClick = () => setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("click", onClick);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("click", onClick); };
  }, [open]);

  // Auto-fade so a failed open doesn't leave a permanent error chip.
  useEffect(() => {
    if (!openError) return;
    const id = setTimeout(() => setOpenError(null), 6000);
    return () => clearTimeout(id);
  }, [openError]);

  const base = (p: string) => workspaceLeafName(p, ws?.home);

  const openPath = async (p: string) => {
    setOpen(false);
    setOpenError(null);
    try {
      const res = await api.openWorkspace(p);
      if ((res as any).ok) {
        refresh();
        window.dispatchEvent(new Event("harness-config-changed"));
      } else if ((res as { code?: string }).code === "lease_exhausted") {
        setOpenError(formatWorkspaceOpenLeaseExhaustedMessage(res));
      } else {
        // A stale recent (deleted/moved folder) used to no-op silently here.
        setOpenError((res as any).error || `Could not open ${base(p)}`);
      }
    } catch (err) {
      if (isWorkspaceOpenLeaseExhausted(err)) {
        setOpenError(formatWorkspaceOpenLeaseExhaustedMessage(err));
      } else {
        setOpenError((err as Error)?.message || `Could not open ${base(p)}`);
      }
    }
  };
  const browse = async () => {
    const picked = await pickFolder();
    if (picked) await openPath(picked);
  };
  const name = ws?.repo ? base(ws.repo) : (ws?.home ? "Home" : "No folder");
  const recents = (ws?.recents || []).filter((r) => r !== ws?.repo);

  return (
    <div className="flex items-center gap-1.5 px-1 pb-1.5 text-[11px] relative">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        className="flex items-center gap-1 text-muted hover:text-txt transition rounded px-1 py-0.5 hover:bg-panel2/60">
        <Folder size={11} className="text-faint" />
        <span className="font-medium">{name}</span>
        <ChevronDown size={11} className="text-faint" />
      </button>
      {ws?.branch ? <span className="text-faint flex items-center gap-0.5"><GitBranch size={10} />{ws.branch}</span> : null}
      <span className="text-faint/70">Local</span>
      {openError && <span className="text-risk/90 truncate max-w-[240px]" title={openError}>{openError}</span>}
      {open && (
        <div onClick={(e) => e.stopPropagation()}
          className="absolute bottom-full left-0 mb-1 w-64 bg-panel border border-edge rounded-lg shadow-xl shadow-black/40 py-1 z-50">
          {recents.length > 0 && (
            <>
              <div className="text-[9px] uppercase tracking-wider text-faint px-3 py-1">Recents</div>
              {recents.map((r) => (
                <button key={r} onClick={() => openPath(r)}
                  className="w-full text-left px-3 py-1.5 hover:bg-panel2 transition flex flex-col">
                  <span className="text-txt font-medium text-[11px]">{base(r)}</span>
                  <span className="text-faint text-[9px] font-mono truncate">{r}</span>
                </button>
              ))}
              <div className="border-t border-edge/50 my-1" />
            </>
          )}
          <button onClick={browse}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 transition flex items-center gap-2 text-txt text-[11px]">
            <Folder size={12} className="text-accent" /> Open Folder...
          </button>
        </div>
      )}
    </div>
  );
}
