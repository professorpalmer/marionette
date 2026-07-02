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

    // Advance the inline progress text from an event payload (shared by the
    // pre-commit download churn and the post-commit install stages). The
    // installed-app updater bakes the percent into the message ("Downloading
    // update 72%"), so only append when the message doesn't already carry one --
    // otherwise you get "... 72% 72%".
    const showProgress = (p: any) => {
      setApplying(true);
      const base = p.message || "Updating";
      const hasPct = /\d%\s*$/.test(base);
      setProgress(base + (p.percent != null && !hasPct ? ` ${p.percent}%` : ""));
    };

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

    // React to live updater events. Every payload carries `version`, so we label
    // the banner from the event alone -- deliberately NOT calling updates:check
    // here, which used to re-trigger checkForUpdates -> another "available" event
    // -> another check, an infinite loop that was the visible "blinking".
    const off = ipc.updates.onProgress((p: any) => {
      if (!p || !p.stage) return;
      if (p.version) setLatest(p.version);

      // A failed download/install: recover to an actionable state instead of a
      // permanent spinner, and reopen the gate so the user can retry.
      if (p.stage === "error") {
        committedRef.current = false;
        setApplying(false);
        if (p.message) window.dispatchEvent(new CustomEvent("harness-toast", { detail: p.message }));
        return;
      }

      // Once the user has committed (clicked Restart), the banner stays in the
      // install view and only advances progress text. A stray background
      // "available"/"downloaded" event must NOT flip it back to "Restart now"
      // mid-install (that flip was another source of blinking).
      if (committedRef.current) {
        showProgress(p);
        return;
      }

      if (p.stage === "available" || p.stage === "downloaded") {
        // Surface the actionable "ready -- Restart now" view. Latch it so late
        // background download-progress can't flip it back and forth.
        readyRef.current = true;
        setReady(true);
        setApplying(false);
        return;
      }

      // Pre-commit background download churn. Once we've latched "ready", ignore
      // it so the banner doesn't oscillate between "Restart now" and progress.
      if (readyRef.current) return;
      showProgress(p);
    });

    return () => {
      cancelled = true;
      if (off) off();
    };
  }, []);

  const restart = () => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    if (committedRef.current) return; // idempotent: a second click while applying is a no-op
    committedRef.current = true;
    setApplying(true);
    setProgress("Preparing update");

    // Recover the banner to an actionable state instead of stranding the user.
    const recover = (msg: string) => {
      committedRef.current = false;
      setApplying(false);
      window.dispatchEvent(new CustomEvent("harness-toast", { detail: msg }));
    };

    // Watchdog: on success the main process swaps the bundle and relaunches, so
    // this window is destroyed well before this fires. If we're still alive after
    // 90s, the install silently stalled -- return to "Restart now" and point the
    // user at the releases page rather than spinning forever.
    const watchdog = window.setTimeout(() => {
      recover("Update is taking longer than expected. Try again, or download it from the releases page.");
      ipc.updates.openRepo?.().catch?.(() => {});
    }, 90000);

    ipc.updates
      .apply()
      .then((r: any) => {
        // apply() resolves { ok:false, error } when there's nothing to install
        // (e.g. a stale banner) -- surface it instead of spinning.
        if (r && r.ok === false) {
          window.clearTimeout(watchdog);
          recover(`Update failed: ${r.error || "no update available"}`);
        }
      })
      .catch((e: any) => {
        window.clearTimeout(watchdog);
        recover(`Update failed: ${String(e)}`);
      });
  };

  if (dismissed || (!ready && !applying)) return null;

  const versionLabel = latest ? (latest.startsWith("v") ? latest : `v${latest}`) : "A new version";

  return (
    // pl-24 clears the macOS traffic-light window controls with a comfortable
    // margin (this banner is the topmost strip, so nothing else reserves that
    // corner). Deliberately NOT a drag region: Electron intermittently swallows
    // clicks on no-drag children inside a drag parent, which made "Restart now"
    // flash its active state without ever firing. A working button beats a
    // draggable transient strip.
    <div
      className="flex items-center gap-3 pl-24 pr-4 py-2 bg-accent/10 border-b border-accent/30 text-[12px] text-txt select-none shrink-0"
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
            className="px-2.5 py-1 rounded-md bg-accent text-panel font-semibold hover:brightness-110 transition text-[11px]"
          >
            Restart now
          </button>
          <button
            onClick={() => setDismissed(true)}
            title="Dismiss (updates on next relaunch)"
            className="p-1 rounded text-muted hover:text-txt hover:bg-edge/40 transition"
          >
            <X size={13} />
          </button>
        </>
      )}
    </div>
  );
}
