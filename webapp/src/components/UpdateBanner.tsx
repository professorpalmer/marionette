import { useEffect, useRef, useState } from "react";
import { ArrowUpCircle, RefreshCw, X } from "lucide-react";
import { sanitizeUpdateMessage } from "../lib/updateMessages";

// The loud counterpart to the StatusBar's small "update" pill. When the tracked
// branch has moved ahead of this checkout, this slides a prominent bar across the
// top of the window -- Hermes-style -- so a waiting update is impossible to miss.
// Restart applies it (git pull + rebuild + relaunch); dismiss hides it until the
// next launch or the next commit lands. Because Marionette can edit its own
// source, apply() may return a structured code (dirty/diverged/conflict) that we
// turn into a real choice (stash + reapply, or point at the diverged commits)
// instead of a dead-end error.
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
      // An older bundled updater streams raw npm/git/pip output as the
      // message; sanitize so deprecation warnings never render as progress.
      const base = sanitizeUpdateMessage(p.stage || "", p.message || "");
      const hasPct = /\d%\s*$/.test(base);
      setProgress(base + (p.percent != null && !hasPct ? ` ${p.percent}%` : ""));
    };

    // Check now, then re-check every 30 minutes and on window focus (throttled)
    // so a release that lands mid-session raises the banner without a relaunch.
    // Never re-check while an apply is in flight -- checkForUpdate runs a git
    // fetch that could race the updater's own fetch/merge.
    let lastCheck = 0;
    const MIN_GAP_MS = 5 * 60 * 1000;
    const check = (force = false) => {
      if (committedRef.current) return;
      const now = Date.now();
      if (!force && now - lastCheck < MIN_GAP_MS) return;
      lastCheck = now;
      ipc.updates
        .check()
        .then((res: any) => {
          if (cancelled || !res || committedRef.current) return;
          if (res.available || res.downloaded) {
            // Only a real version string labels the banner. Source-run updates
            // track a branch tip, and falling back to the branch name rendered
            // the nonsense "vmain of Marionette is ready".
            setLatest(res.latest || "");
            readyRef.current = true;
            setReady(true);
          }
        })
        .catch(() => {});
    };
    check(true);
    const interval = window.setInterval(() => check(true), 30 * 60 * 1000);
    const onFocus = () => check();
    window.addEventListener("focus", onFocus);

    // PUSH path: the Electron main process runs its own update watcher and
    // notifies us the moment a background fetch finds new commits -- no need to
    // wait for the next renderer poll tick (which historically meant the banner
    // only appeared after a full app restart).
    const offAvailable = ipc.updates.onAvailable
      ? ipc.updates.onAvailable((res: any) => {
          if (cancelled || !res || committedRef.current) return;
          setLatest(res.latest || "");
          readyRef.current = true;
          setReady(true);
        })
      : null;

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
        window.dispatchEvent(new Event("harness-update-idle")); // release the pill mirror too
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
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      if (off) off();
      if (offAvailable) offAvailable();
    };
  }, []);

  const restart = (strategy?: "ff" | "stash") => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    if (committedRef.current) return; // idempotent: a second click while applying is a no-op
    committedRef.current = true;
    setApplying(true);
    setProgress("Preparing update");
    // Tell the StatusBar pill we're committing so both surfaces show the install
    // view in lockstep (the banner is the sole apply() driver).
    window.dispatchEvent(new Event("harness-update-committing"));

    // Recover the banner to an actionable state instead of stranding the user.
    const recover = (msg: string) => {
      committedRef.current = false;
      setApplying(false);
      window.dispatchEvent(new Event("harness-update-idle")); // release the pill mirror too
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
      .apply(strategy ? { strategy } : undefined)
      .then((r: any) => {
        // apply() resolves { ok:false, error } when there's nothing to install
        // (e.g. a stale banner) -- surface it instead of spinning.
        if (r && r.ok === false) {
          window.clearTimeout(watchdog);
          // A self-edited checkout collides with fast-forward. Offer the sane
          // recovery for each case instead of a dead-end error (Marionette
          // edits its own source, so this is a normal path, not an edge case).
          if (r.code === "dirty") {
            recover(r.error || "You have local self-edits.");
            if (window.confirm(
              "You have local self-edits in your Marionette checkout.\n\n" +
              "Stash them, update, then reapply them automatically?"
            )) {
              restart("stash");
            }
            return;
          }
          if (r.code === "diverged" || r.code === "conflict") {
            recover(r.error || "Your checkout has diverged from origin.");
            ipc.updates.openRepo?.("commits").catch?.(() => {});
            return;
          }
          recover(`Update failed: ${r.error || "no update available"}`);
        }
      })
      .catch((e: any) => {
        window.clearTimeout(watchdog);
        recover(`Update failed: ${String(e)}`);
      });
  };

  // The StatusBar pill delegates here so there is exactly one apply() driver: a
  // pill click dispatches harness-update-apply and we run the same robust
  // restart() path. restart() only touches stable refs/state setters, so binding
  // the listener once is safe.
  useEffect(() => {
    const onApplyRequest = () => restart();
    window.addEventListener("harness-update-apply", onApplyRequest);
    return () => window.removeEventListener("harness-update-apply", onApplyRequest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
            onClick={() => restart()}
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
