import { useLayoutEffect, useState } from "react";
import { getHarnessIpc } from "../lib/transport";

interface ComputerState {
  sessionId: string;
  apps: { app_id: string; name: string }[];
  pending: boolean;
}
interface ComputerBridge {
  setComputerSession?: (sessionId: string) => Promise<void>;
  releaseComputerSession?: (sessionId: string) => Promise<void>;
  revokeComputerAccess?: () => Promise<void>;
  onComputerState?: (callback: (state: ComputerState) => void) => () => void;
  onOpenBrowser?: (callback: (sessionId: string) => void) => () => void;
}

export default function ComputerAccess({
  sessionId,
  onOpenBrowser,
  onStealSession,
}: {
  sessionId: string;
  onOpenBrowser: () => void;
  onStealSession?: (sessionId: string) => void;
}) {
  const [state, setState] = useState<ComputerState | null>(null);
  const ipc: ComputerBridge | undefined = getHarnessIpc();
  useLayoutEffect(() => {
    const off = ipc?.onComputerState?.(setState);
    void ipc?.setComputerSession?.(sessionId);
    return () => {
      off?.();
      void ipc?.releaseComputerSession?.(sessionId);
    };
  }, [ipc, sessionId]);
  useLayoutEffect(() => ipc?.onOpenBrowser?.(requestedSession => {
    if (requestedSession === sessionId) onOpenBrowser();
    else if (requestedSession && onStealSession) onStealSession(requestedSession);
  }), [ipc, sessionId, onOpenBrowser, onStealSession]);
  if (!state || state.sessionId !== sessionId || (!state.pending && !state.apps.length)) return null;
  return <div role="status" className="flex items-center justify-between gap-3 px-4 py-2 text-sm bg-[var(--shell-chrome)] text-[var(--text-primary)] border-b border-[var(--shell-panel-border)]">
    <span>{state.pending ? "Computer access awaiting approval" : `Computer access: ${state.apps.map(app => app.name).join(", ")}`}</span>
    <button type="button" className="underline focus-visible:outline" onClick={() => { void ipc?.revokeComputerAccess?.(); }}>Stop computer control</button>
  </div>;
}
