import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionSwitch, type UseSessionSwitchDeps } from "../components/conversation/useSessionSwitch";
import type { Item } from "../components/TranscriptList";
import type { ComposerAttachedImage } from "../components/conversation/composerAttachmentCache";
import type { RecoveryContext } from "../lib/turnTerminal";
import { api } from "../lib/api";
import { clearTranscriptCache, writeTranscriptCache } from "../components/conversation/transcriptCache";
vi.mock("../lib/api", () => ({ api: {
 sessionTranscript: vi.fn(), getSessionState: vi.fn(), swarmLive: vi.fn(),
 readEventsSince: vi.fn(), chatEventsLive: vi.fn(), interruptSession: vi.fn(),
} }));
const ref = <T,>(current: T) => ({ current });
const msg = (text: string, id?: string): Item => ({
  kind: "msg",
  msg: { role: "user", text, ...(id ? { id } : {}) },
});
/** Hydrate stamps ordinal ids for session A user rows. */
const hydratedUser = (text: string, ordinal = 0): Item =>
  msg(text, `msg:A:user:${ordinal}`);
function deferred<T>() {
 let resolve!: (value: T) => void;
 const promise = new Promise<T>((r) => { resolve = r; });
 return { promise, resolve };
}
function fixture() {
 const deps: UseSessionSwitchDeps = {
  activeSessionId: "A",
  onArtifacts: vi.fn(),
  clearChatEventsPoll: vi.fn(),
  itemsRef: ref<Item[]>([]),
  transcriptStaleRef: ref(false),
  cachedSessionIdRef: ref<string | null>(null),
  transcriptLoadGenRef: ref(0),
  transcriptFpRef: ref(""),
  streamGenRef: ref(0),
  streamSessionIdRef: ref<string | null>(null),
  lastAppliedCursorRef: ref(0),
  lastAppliedRingCursorRef: ref(0),
  ringGenerationRef: ref<number | undefined>(undefined),
  chatEventsPollTimerRef: ref<number | null>(null),
  chatEventsLiveCancelRef: ref<null | (() => void)>(null),
  applyStreamEventRef: ref(vi.fn()),
  flushTypewriterRef: ref(vi.fn()),
  maybeRunQueuedResumeRef: ref(vi.fn()),
  maybeDrainQueueRef: ref(vi.fn()),
  ensureChatEventsReattachRef: ref(vi.fn()),
  cancelRef: ref<null | (() => void)>(null),
  localStreamActiveRef: ref(false),
  detachedBusyRef: ref(false),
  userStoppedRef: ref(false),
  turnSettledRef: ref(false),
  abandonStaleLocalStreamRef: ref(vi.fn()),
  resumeQueuedRef: ref(false),
  approvedCommandRetryRef: ref<string | null>(null),
  runnerBusyPollGenRef: ref(0),
  typeRafRef: ref<number | null>(null),
  typeBufRef: ref(""),
  typeDoneRef: ref(false),
  setItems: vi.fn(),
  setTranscriptStale: vi.fn(),
  setTurnOpen: vi.fn(),
  setStatus: vi.fn(),
  setCompactingStatus: vi.fn(),
  setEditingIndex: vi.fn(),
  setCanRevertEdit: vi.fn(),
  setEditNotice: vi.fn(),
  setEditBusy: vi.fn(),
  setInput: vi.fn(),
  composerInputRef: ref(""),
  setAttachedImages: vi.fn(),
  attachedImagesRef: ref<ComposerAttachedImage[]>([]),
  setWikiPrepared: vi.fn(),
  setMemoryProposals: vi.fn(),
  setDistillNotice: vi.fn(),
  setUploadError: vi.fn(),
  setWaitHint: vi.fn(),
  setPendingJobIds: vi.fn(),
  setBackendPendingSwarms: vi.fn(),
  setSessionSwitchPending: vi.fn(),
  clearSafeTimeouts: vi.fn(),
  setTurnLifecycle: vi.fn(),
  setTerminalCause: vi.fn(),
  recoveryDispatchingRef: ref(false),
  recoveryContextRef: ref<RecoveryContext | null>(null),
 };
 deps.setItems = (update) => { deps.itemsRef.current = typeof update === "function" ? update(deps.itemsRef.current) : update; };
 deps.clearChatEventsPoll = () => {
  if (deps.chatEventsPollTimerRef.current != null) window.clearTimeout(deps.chatEventsPollTimerRef.current);
  deps.chatEventsPollTimerRef.current = null;
  deps.chatEventsLiveCancelRef.current?.();
  deps.chatEventsLiveCancelRef.current = null;
 };
 return deps;
}
beforeEach(() => {
 vi.useFakeTimers(); clearTranscriptCache();
 vi.mocked(api.getSessionState).mockResolvedValue({ runners: {} });
 vi.mocked(api.swarmLive).mockResolvedValue({ jobs: [] });
 vi.mocked(api.readEventsSince).mockImplementation(() => new Promise(() => {}));
 vi.mocked(api.chatEventsLive).mockReturnValue(vi.fn());
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); });
it.each(["append", "clear", "edit"])("rejects late hydrate after local %s", async (kind) => {
 writeTranscriptCache("A", [msg("old")]);
 const response = deferred<Awaited<ReturnType<typeof api.sessionTranscript>>>();
 vi.mocked(api.sessionTranscript).mockReturnValue(response.promise);
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 const local = kind === "clear" ? [] : [msg(kind)];
 act(() => d.setItems(local));
 await act(async () => response.resolve({ history: [{role: "user", content: "old"}] }));
 expect(d.itemsRef.current).toEqual(local);
});
it("arms recovery after all transcript failures and can recover later", async () => {
 vi.mocked(api.sessionTranscript).mockRejectedValue(new Error("offline"));
 const recovery = deferred<Awaited<ReturnType<typeof api.readEventsSince>>>();
 vi.mocked(api.readEventsSince).mockReturnValueOnce(recovery.promise);
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(3000));
 expect(api.readEventsSince).toHaveBeenCalledTimes(1);
 d.ensureChatEventsReattachRef.current();
 await act(async () => {});
 expect(api.readEventsSince).toHaveBeenCalledTimes(1);
 vi.mocked(api.sessionTranscript).mockResolvedValue({history:[{role:"user",content:"recovered"}]});
 await act(async () => recovery.resolve({session_id:"A",cursor:0,events:[{id:0,kind:"ring_miss",data:{code:"ring_miss",missed:true,available:false}}]}));
 expect(d.itemsRef.current).toEqual([hydratedUser("recovered")]);
});
it("arms recovery after an empty warm refresh", async () => {
 writeTranscriptCache("A", [msg("warm")]);
 vi.mocked(api.sessionTranscript).mockResolvedValue({history: []});
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(1500));
 expect(d.itemsRef.current).toEqual([msg("warm")]);
 expect(api.readEventsSince).toHaveBeenCalledTimes(1);
});
it("cold resume hydrates then attaches exactly one live owner", async () => {
 vi.mocked(api.sessionTranscript).mockResolvedValue({history: [{role:"user", content:"cold"}]});
 vi.mocked(api.getSessionState).mockResolvedValue({ runners: { A: "running" } });
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => {});
 expect(d.itemsRef.current).toEqual([hydratedUser("cold")]);
 expect(api.chatEventsLive).toHaveBeenCalledTimes(1);
 expect(api.readEventsSince).not.toHaveBeenCalled();
 d.ensureChatEventsReattachRef.current();
 await act(async () => {});
 expect(api.chatEventsLive).toHaveBeenCalledTimes(1);
});
it("rapid A-B-A rejects old responses and only detaches the backend stream", async () => {
 const a = deferred<Awaited<ReturnType<typeof api.sessionTranscript>>>();
 const b = deferred<Awaited<ReturnType<typeof api.sessionTranscript>>>();
 const latest = deferred<Awaited<ReturnType<typeof api.sessionTranscript>>>();
 vi.mocked(api.sessionTranscript).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(latest.promise);
 const d = fixture(); const close = vi.fn(); d.cancelRef.current = close;
 const h = renderHook(({sid}) => useSessionSwitch({...d, activeSessionId:sid}), {initialProps:{sid:"A"}});
 h.rerender({sid:"B"}); h.rerender({sid:"A"});
 await act(async () => latest.resolve({history:[{role:"user",content:"latest"}]}));
 await act(async () => { a.resolve({history:[{role:"user",content:"old A"}]}); b.resolve({history:[{role:"user",content:"old B"}]}); });
 expect(d.itemsRef.current).toEqual([hydratedUser("latest")]);
 expect(close).toHaveBeenCalledTimes(1);
 expect(api.interruptSession).not.toHaveBeenCalled();
});

it("accepts an authoritative clear when no local mutation intervened", async () => {
 vi.mocked(api.sessionTranscript).mockResolvedValue({history:[]});
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(1500));
 expect(d.itemsRef.current).toEqual([]);
 expect(d.setTranscriptStale).toHaveBeenLastCalledWith(false);
});
it("failed cold hydrate cannot erase a newly composed row", async () => {
 vi.mocked(api.sessionTranscript).mockRejectedValue(new Error("offline"));
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 act(() => d.setItems([msg("new")]));
 await act(async () => vi.advanceTimersByTimeAsync(3000));
 expect(d.itemsRef.current).toEqual([msg("new")]);
});

it("recovers a failed cold transcript while the idle event store reports no ring miss", async () => {
 vi.mocked(api.sessionTranscript).mockRejectedValue(new Error("offline"));
 vi.mocked(api.readEventsSince).mockResolvedValue({session_id:"A", cursor:0, events:[]});
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(3000));
 expect(d.itemsRef.current).toEqual([]);
 vi.mocked(api.sessionTranscript).mockResolvedValue({history:[{role:"user",content:"durable"}]});
 await act(async () => vi.advanceTimersByTimeAsync(2000));
 expect(d.itemsRef.current).toEqual([hydratedUser("durable")]);
});
it("bounds failed cold recovery", async () => {
 vi.mocked(api.sessionTranscript).mockRejectedValue(new Error("offline"));
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(30000));
 expect(api.sessionTranscript).toHaveBeenCalledTimes(12);
 await act(async () => vi.advanceTimersByTimeAsync(30000));
 expect(api.sessionTranscript).toHaveBeenCalledTimes(12);
});
it("re-arms the store owner after a local send advances the stream generation", async () => {
 vi.mocked(api.sessionTranscript).mockResolvedValue({history:[{role:"user",content:"old"}]});
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => {});
 vi.mocked(api.readEventsSince).mockClear();
 d.clearChatEventsPoll();
 d.streamGenRef.current += 1;
 d.localStreamActiveRef.current = true;
 d.ensureChatEventsReattachRef.current();
 expect(api.readEventsSince).not.toHaveBeenCalled();
 d.localStreamActiveRef.current = false;
 await act(async () => d.ensureChatEventsReattachRef.current());
 expect(api.readEventsSince).toHaveBeenCalledTimes(1);
});

it("cancels scheduled cold recovery after a local edit", async () => {
 vi.mocked(api.sessionTranscript).mockRejectedValue(new Error("offline"));
 const d = fixture(); renderHook(() => useSessionSwitch(d));
 await act(async () => vi.advanceTimersByTimeAsync(3000));
 act(() => d.setItems([msg("local")]));
 await act(async () => vi.advanceTimersByTimeAsync(30000));
 expect(api.sessionTranscript).toHaveBeenCalledTimes(4);
 expect(d.itemsRef.current).toEqual([msg("local")]);
});
it("a store replay asked for during the transcript load waits for the load", async () => {
 // Measured in the rig: on reload the replay started while the transcript
 // load was in flight. It painted the newest turn's tail on the empty feed,
 // the load was then dropped as stale, and only the tail stayed.
 const response = deferred<Awaited<ReturnType<typeof api.sessionTranscript>>>();
 vi.mocked(api.sessionTranscript).mockReturnValue(response.promise);
 vi.mocked(api.readEventsSince).mockResolvedValue({ session_id: "A", cursor: 1, events: [
  { id: 1, kind: "stream", data: { kind: "message", text: "replayed tail", cursor: 1, generation: 1 } },
 ] } as Awaited<ReturnType<typeof api.readEventsSince>>);
 const d = fixture();
 d.applyStreamEventRef.current = () => d.setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: "replayed tail" } }]);
 renderHook(() => useSessionSwitch(d));
 d.ensureChatEventsReattachRef.current();
 await act(async () => {});
 expect(api.readEventsSince).not.toHaveBeenCalled();
 await act(async () => response.resolve({ history: [{ role: "user", content: "first turn" }] }));
 await act(async () => {});
 expect(d.itemsRef.current[0]).toEqual(hydratedUser("first turn"));
 expect(api.readEventsSince).toHaveBeenCalled();
});
