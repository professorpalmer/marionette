export type GitRefreshContext = Readonly<{ epoch: number; path: string }>;
export type GitRefreshLane = "status" | "branches";
export type GitRefreshPriority = "immediate" | "automatic";

type Batch = {
  context: GitRefreshContext;
  priority: GitRefreshPriority;
  complete: (() => void)[];
};
type Lane = {
  running: Batch | null;
  pending: Batch | null;
  timer: ReturnType<typeof setTimeout> | null;
};
type Options = {
  run: (lane: GitRefreshLane, context: GitRefreshContext, priority: GitRefreshPriority) => Promise<void>;
  onError: (error: unknown, lane: GitRefreshLane, context: GitRefreshContext) => void;
  onBusy: (busy: boolean) => void;
};

export function createGitRefreshCoordinator(options: Options) {
  const lanes: Record<GitRefreshLane, Lane> = {
    status: { running: null, pending: null, timer: null },
    branches: { running: null, pending: null, timer: null },
  };
  let epoch = 0;
  let current: GitRefreshContext | null = null;
  let lastAutomaticStart = -Infinity;
  const isCurrent = (context: GitRefreshContext) => current?.epoch === context.epoch;
  const busy = () => {
    if (current) options.onBusy(Object.values(lanes).some(lane =>
      lane.running?.context.epoch === current?.epoch));
  };
  function clearPending(lane: Lane) {
    if (lane.timer !== null) clearTimeout(lane.timer);
    lane.timer = null;
    lane.pending?.complete.forEach(resolve => resolve());
    lane.pending = null;
  }
  function drain(name: GitRefreshLane) {
    const lane = lanes[name];
    if (lane.running || !lane.pending) return;
    const batch = lane.pending;
    if (!isCurrent(batch.context)) { clearPending(lane); return; }
    const delay = name === "status" && batch.priority === "automatic"
      ? lastAutomaticStart + 500 - performance.now() : 0;
    if (delay > 0) {
      if (lane.timer === null) lane.timer = setTimeout(() => {
        lane.timer = null;
        drain(name);
      }, delay);
      return;
    }
    if (lane.timer !== null) clearTimeout(lane.timer);
    lane.timer = null;
    lane.pending = null;
    lane.running = batch;
    if (name === "status" && batch.priority === "automatic") lastAutomaticStart = performance.now();
    busy();
    void (async () => {
      try { await options.run(name, batch.context, batch.priority); }
      catch (error: unknown) {
        if (isCurrent(batch.context)) options.onError(error, name, batch.context);
      } finally {
        lane.running = null;
        batch.complete.forEach(resolve => resolve());
        drain(name);
        busy();
      }
    })();
  }
  return {
    isCurrent,
    activate(path: string): GitRefreshContext {
      current = { path, epoch: ++epoch };
      Object.values(lanes).forEach(clearPending);
      busy();
      return current;
    },
    deactivate() {
      ++epoch;
      current = null;
      Object.values(lanes).forEach(clearPending);
    },
    request(context: GitRefreshContext, names: readonly GitRefreshLane[], priority: GitRefreshPriority = "immediate"): Promise<void> {
      if (!isCurrent(context)) return Promise.resolve();
      return Promise.all(names.map(name => new Promise<void>(resolve => {
        const lane = lanes[name];
        if (!lane.pending) lane.pending = { context, priority, complete: [] };
        if (priority === "immediate") lane.pending.priority = "immediate";
        lane.pending.complete.push(resolve);
        drain(name);
      }))).then(() => undefined);
    },
  };
}
