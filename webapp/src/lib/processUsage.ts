import { useEffect, useState } from "react";
import { api, type UsageData } from "./api";

export type ProcessUsageSession = UsageData["session"];

export type ProcessUsageSnapshot = {
  session: ProcessUsageSession | null;
  fetchedAt: number;
  generation: number;
};

type Listener = (snapshot: ProcessUsageSnapshot) => void;

const listeners = new Set<Listener>();

let snapshot: ProcessUsageSnapshot = emptySnapshot();
let inFlight: Promise<void> | null = null;
let acceptZero = false;
let pollTimer: number | undefined;
let subscriberCount = 0;
let busyCount = 0;
let bridgesInstalled = false;

function emptySnapshot(): ProcessUsageSnapshot {
  return { session: null, fetchedAt: 0, generation: 0 };
}

function sessionIsZero(session: ProcessUsageSession): boolean {
  return (session.tokens_used ?? 0) === 0 && (session.est_cost_usd ?? 0) === 0;
}

function sessionHasSpend(session: ProcessUsageSession | null): boolean {
  return Boolean(
    session && ((session.tokens_used ?? 0) > 0 || (session.est_cost_usd ?? 0) > 0),
  );
}

function emit(next: ProcessUsageSnapshot): void {
  snapshot = next;
  listeners.forEach((listener) => listener(snapshot));
}

function acceptSession(session: ProcessUsageSession): void {
  if (acceptZero) {
    acceptZero = false;
  } else if (sessionIsZero(session) && sessionHasSpend(snapshot.session)) {
    return;
  }
  emit({
    session,
    fetchedAt: Date.now(),
    generation: snapshot.generation + 1,
  });
}

export function getProcessUsage(): ProcessUsageSnapshot {
  return snapshot;
}

export function refreshProcessUsage(): Promise<void> {
  if (inFlight) return inFlight;
  const run = (async () => {
    try {
      const data = await api.getUsage();
      if (data?.session) acceptSession(data.session);
    } catch (err) {
      console.error("Failed to load process usage", err);
    } finally {
      inFlight = null;
    }
  })();
  inFlight = run;
  return run;
}

function resetForSessionChange(): void {
  acceptZero = true;
  emit({
    session: null,
    fetchedAt: Date.now(),
    generation: snapshot.generation + 1,
  });
  void refreshProcessUsage();
}

function pollIntervalMs(): number {
  return busyCount > 0 ? 2000 : 10000;
}

function stopPolling(): void {
  if (typeof window === "undefined" || pollTimer === undefined) return;
  window.clearTimeout(pollTimer);
  pollTimer = undefined;
}

function schedulePoll(): void {
  if (typeof window === "undefined") return;
  stopPolling();
  if (subscriberCount <= 0) return;
  pollTimer = window.setTimeout(() => {
    if (typeof document !== "undefined" && document.hidden) {
      schedulePoll();
      return;
    }
    void refreshProcessUsage().finally(() => {
      schedulePoll();
    });
  }, pollIntervalMs());
}

function installBridges(): void {
  if (bridgesInstalled || typeof window === "undefined") return;
  bridgesInstalled = true;
  const refresh = () => {
    void refreshProcessUsage();
  };
  window.addEventListener("harness-usage-refresh", refresh);
  window.addEventListener("harness-config-changed", refresh);
  window.addEventListener("harness-project-selected", refresh);
  window.addEventListener("harness-new-session", resetForSessionChange);
  window.addEventListener("harness-session-changed", resetForSessionChange);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

export function subscribeProcessUsage(listener: Listener): () => void {
  installBridges();
  listeners.add(listener);
  subscriberCount += 1;
  if (subscriberCount === 1) {
    void refreshProcessUsage();
    schedulePoll();
  }
  listener(snapshot);
  return () => {
    listeners.delete(listener);
    subscriberCount = Math.max(0, subscriberCount - 1);
    if (subscriberCount === 0) stopPolling();
  };
}

export function useProcessUsage(opts?: { busy?: boolean }): ProcessUsageSnapshot {
  const [current, setCurrent] = useState(getProcessUsage);
  useEffect(() => subscribeProcessUsage(setCurrent), []);
  useEffect(() => {
    if (!opts?.busy) return undefined;
    busyCount += 1;
    schedulePoll();
    return () => {
      busyCount = Math.max(0, busyCount - 1);
      schedulePoll();
    };
  }, [opts?.busy]);
  return current;
}

export function _resetProcessUsageForTests(): void {
  stopPolling();
  listeners.clear();
  subscriberCount = 0;
  busyCount = 0;
  inFlight = null;
  acceptZero = false;
  snapshot = emptySnapshot();
}
