/**
 * Composer wait-hint hygiene for Hermes/driver route retries.
 *
 * Drivers emit notices like ``driver openrouter:deepseek failed`` while the
 * harness retries another route. Progress events must clear stale failure
 * chrome so a recovered turn does not keep lying in the busy footer.
 */

const PROVIDER_FAILURE_HINT_RE = /^driver\s+.+\s+failed\b/i;

/** True for driver-route failure notices painted as composer wait hints. */
export function isProviderFailureWaitHint(hint: string | null | undefined): boolean {
  const text = String(hint || "").trim();
  if (!text) return false;
  return PROVIDER_FAILURE_HINT_RE.test(text);
}

/** Drop stale provider failure hints; leave swarm await footnotes alone. */
export function clearProviderFailureWaitHint(prev: string | null): string | null {
  return isProviderFailureWaitHint(prev) ? null : prev;
}

/**
 * Suppress a stale failure hint once the turn shows live progress again.
 * Keep the failure visible when the turn actually ended in error.
 */
export function waitHintForBusyProgress(
  hint: string | null | undefined,
  opts: { hasSignal: boolean; turnFailed?: boolean },
): string | null {
  const text = String(hint || "").trim();
  if (!text) return null;
  if (isProviderFailureWaitHint(text) && opts.hasSignal && !opts.turnFailed) {
    return null;
  }
  return text;
}

/** Format a driver failure notice for SSE wait chrome (harness + driver contract). */
export function formatDriverFailureWaitHint(driver: string): string {
  const label = String(driver || "").trim() || "provider";
  return `driver ${label} failed`;
}
