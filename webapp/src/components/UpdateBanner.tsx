import { useEffect, useRef, useState } from "react";
import { ArrowUpCircle, RefreshCw, X } from "lucide-react";

// The loud counterpart to the StatusBar's small "update" pill. When a new
// release has been downloaded in the background (electron-updater, autoDownload),
// this slides a prominent bar across the top of the window -- Hermes-style --
// so a waiting update is impossible to miss. Restart applies it (bundle swap +
// relaunch); dismiss hides it until the next launch or the next release lands.
//
// The pill stays as the always-present compact indicator; this banner is the
// occasional "your update is ready, one click to finish" nudge.
export default function UpdateBanner() {
  const [latest, setLatest] = useState<string>("");
  const [ready, setReady] = useState(false);
  const [applying, setApplying] = useState(false);
  const [progress, setProgress] = useState<string>("");
  const [dismissed, setDismissed] = useState(false);
  // Refs so the once-mounted event handler reads live state without re-subscribing.
  // readyRef latches: once an update is downloaded, stray late download-progress
  // events must not flip the banner back to "Downloading X%" (that flip-flop was
  // the visible blinking). committedRef opens the gate again after the user hits
  // Restart, so genuine post-commit install/relaunch stages still show progress.
  const readyRef = useRef(false);
  const committedRef = useRef(false);

  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;

    let cancelled = false;

    // Catch an update that already finished downloading before this mounted.
    ipc.updates
      .check()
      .then((res: any) => {
        if (cancelled || !res) return;
        if (res.available || res.downloaded) {
          setLatest(res.latest || res.branch || "");
          readyRef.current = true;
          setReady(true);
        }
      })
      .catch(() => {});

    // React to live updater events. "available"/"downloaded" surface the banner;
    // the install stages drive the inline progress text once the user commits.
    const off = ipc.updates.onProgress((p: any) => {
      if (!p || !p.stage) return;
      // "available"/"downloaded" surface the banner; every other stage
      // (prepare/download/install/done for the binary updater, or
      // fetch/rebuild/relaunch for the git self-updater) means the user has
      // committed and we show inline progress.
      if (p.stage === "available" || p.stage === "downloaded") {
        readyRef.current = true;
        setReady(true);
        // Download finished (or an update is simply available): leave the
        // "applying" progress state so the banner flips to the actionable
        // "ready -- Restart now" view. Without this it stuck at "Downloading
        // update 100%" (the applying branch has no button) until the user found
        // the StatusBar pill.
        setApplying(false);
        // Refresh the version label cleanly rather than parsing the message.
        ipc.updates.check().then((res: any) => {
          if (!cancelled && res) setLatest(res.latest || res.branch || "");
        }).catch(() => {});
      } else {
        // Once ready and not yet committed by the user, ignore download-progress
        // churn -- otherwise a late "Downloading 100%" event would flip the
        // banner off the "Restart now" view and back, which reads as blinking.
        if (readyRef.current && !committedRef.current) return;
        setApplying(true);
        // Append the percent only when the message doesn't already carry one.
        // The installed-app updater bakes it into the text ("Downloading update
        // 72%"), so blindly appending produced "... 72% 72%". The git
        // self-updater's messages ("Fetching latest changes") have no percent
        // and still rely on this append for progress.
        const base = p.message || "Updating";
        const hasPct = /\d%\s*$/.test(base);
        setProgress(base + (p.percent != null && !hasPct ? ` ${p.percent}%` : ""));
      }
    });

    return () => {
      cancelled = true;
      if (off) off();
    };
  }, []);

  const restart = () => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    committedRef.current = true;
    setApplying(true);
    setProgress("Preparing update");
    ipc.updates.apply().catch((e: any) => {
      setApplying(false);
      window.dispatchEvent(new CustomEvent("harness-toast", { detail: `Update failed: ${String(e)}` }));
    });
    // On success the main process relaunches; this window goes away.
  };

  if (dismissed || (!ready && !applying)) return null;

  const versionLabel = latest ? (latest.startsWith("v") ? latest : `v${latest}`) : "A new version";

  return (
    // pl-24 clears the macOS traffic-light window controls with a comfortable
    // margin (this banner is the topmost strip, so nothing else reserves that
    // corner). The whole bar is a drag region so the window can still be moved
    // from the top; interactive controls opt back out with no-drag.
    <div
      className="flex items-center gap-3 pl-24 pr-4 py-2 bg-accent/10 border-b border-accent/30 text-[12px] text-txt select-none shrink-0"
      style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
    >
      <ArrowUpCircle size={15} className="text-accent shrink-0" />
      {applying ? (
        <span className="flex items-center gap-2 text-txt">
          <RefreshCw size={12} className="animate-spin text-accent" />
          <span>{progress || "Updating"}</span>
        </span>
      ) : (
        <>
          <span className="font-medium">
            {versionLabel} of Marionette is ready.
          </span>
          <span className="text-muted">Restart to finish updating.</span>
          <div className="flex-1" />
          <button
            onClick={restart}
            style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}
            className="px-2.5 py-1 rounded-md bg-accent text-panel font-semibold hover:brightness-110 transition text-[11px]"
          >
            Restart now
          </button>
          <button
            onClick={() => setDismissed(true)}
            title="Dismiss (updates on next relaunch)"
            style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}
            className="p-1 rounded text-muted hover:text-txt hover:bg-edge/40 transition"
          >
            <X size={13} />
          </button>
        </>
      )}
    </div>
  );
}
