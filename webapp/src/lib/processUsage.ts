import { useEffect, useState } from "react";
import { api, type UsageData } from "./api";

export type ProcessUsageSession = UsageData["session"];

export type ProcessUsageSnapshot = {
  session: ProcessUsageSession | null;
  readStatus?: "unavailable";
  sessionTotal?: UsageData["session_total"];
  fetchedAt: number;
  generation: number;
};

type Listener = (snapshot: ProcessUsageSnapshot) => void;

const listeners = new Set<Listener>();

let snapshot: ProcessUsageSnapshot = emptySnapshot();
let inFlight: Promise<void> | null = null;
let acceptZero = false;
let scopeGeneration = 0;
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

function acceptSession(session: ProcessUsageSession, sessionTotal: UsageData["session_total"]): void {
  if (session.read_status === "unavailable") {
    emit({ session, sessionTotal, readStatus: "unavailable", fetchedAt: Date.now(), generation: snapshot.generation + 1 });
    return;
  }
  if (acceptZero) {
    acceptZero = false;
  } else if (sessionIsZero(session) && sessionHasSpend(snapshot.session) && snapshot.readStatus !== "unavailable" && session.cost_source !== "provider") {
    emit({ ...snapshot, sessionTotal });
    return;
  }
  emit({
    session,
    sessionTotal,
    fetchedAt: Date.now(),
    generation: snapshot.generation + 1,
  });
}

export function getProcessUsage(): ProcessUsageSnapshot {
  return snapshot;
}

/** The footer and This session / All time share this persisted session projection. */
export function activeSessionUsage(current: ProcessUsageSnapshot): ProcessUsageSession | null {
  const total = current.sessionTotal;
  if (!total?.session_id) return null;
  return {
    ...total,
    accounting_scope: 'conversation',
    tokens_used: total.tokens_used ?? total.input_tokens + total.output_tokens,
    driver: '', price_in: 0, price_out: 0,
    estimated: total.estimated ?? true,
    cost_source: total.cost_source ?? 'estimated',
    list_price_complete: total.list_price_complete ?? false,
  };
}

export function refreshProcessUsage(): Promise<void> {
  if (inFlight) return inFlight;
  const owner = scopeGeneration;
  const run = (async () => {
    try {
      const data = await api.getUsage();
      if (owner !== scopeGeneration) return;
      if (!data?.session) throw new Error("Usage unavailable");
      acceptSession(data.session, data.session_total);
    } catch (err) {
      if (owner === scopeGeneration) {
        emit({ session: null, readStatus: "unavailable", fetchedAt: Date.now(), generation: snapshot.generation + 1 });
      }
    } finally {
      if (owner === scopeGeneration) inFlight = null;
    }
  })();
  inFlight = run;
  return run;
}

function resetForSessionChange(): void {
  scopeGeneration += 1;
  inFlight = null;
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
  window.addEventListener("harness-project-selected", resetForSessionChange);
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
  scopeGeneration += 1;
  snapshot = emptySnapshot();
}
