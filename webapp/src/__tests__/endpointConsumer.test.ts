import { expect, it, vi } from 'vitest';
import { createChatEventsReattach, type ChatEventsReattachDeps } from '../components/conversation/chatEventsReattach';
import { api } from '../lib/api';
vi.mock('../lib/api',()=>({api:{readEventsSince:vi.fn()}}));
function deps(): ChatEventsReattachDeps {
  return {
    cancelled:()=>false, loadGen:1, transcriptLoadGenRef:{current:1}, streamGenRef:{current:1},
    reattachGen:1, reattachSid:'s', cachedSessionIdRef:{current:'s'}, localStreamActiveRef:{current:false},
    userStoppedRef:{current:false}, lastAppliedCursorRef:{current:99},lastAppliedRingCursorRef:{current:99},
    ringGenerationRef:{current:9},detachedBusyRef:{current:true},runnerBusyPollGenRef:{current:1},
    itemsRef:{current:[]},transcriptFpRef:{current:''},chatEventsPollTimerRef:{current:null},chatEventsLiveCancelRef:{current:null},
    applyStreamEventRef:{current:vi.fn()},flushTypewriterRef:{current:vi.fn()},maybeRunQueuedResumeRef:{current:vi.fn()},
    maybeDrainQueueRef:{current:vi.fn()},clearChatEventsPoll:vi.fn(),setItems:vi.fn(),setTranscriptStale:vi.fn(),
    setTurnOpen:vi.fn(),setStatus:vi.fn(),
  };
}
it('clears old consumer cursors before applying a new endpoint replay', async()=>{
  const d=deps();
  vi.mocked(api.readEventsSince).mockResolvedValue({session_id:'s',stream_id:'opaque',cursor:1,replay_reset:true,
    events:[{id:1,kind:'stream',session_id:'s',data:{kind:'token',data:'new',cursor:1,generation:2}}]});
  await createChatEventsReattach(d).pullChatEvents();
  expect(d.applyStreamEventRef.current).toHaveBeenCalledWith({kind:'token',data:'new'});
  expect(d.lastAppliedCursorRef.current).toBe(1);
  // The applied frame is the new ring position, so a live watch starts after it.
  expect(d.lastAppliedRingCursorRef.current).toBe(1);
  expect(d.ringGenerationRef.current).toBe(2);
});
it('a late replay cannot clear or paint the newly selected session',async()=>{
  const d=deps();
  vi.mocked(api.readEventsSince).mockImplementation(async()=>{
    d.cachedSessionIdRef.current='other';
    return {session_id:'s',stream_id:'opaque',cursor:1,replay_reset:true,events:[]};
  });
  await createChatEventsReattach(d).pullChatEvents();
  expect(d.lastAppliedCursorRef.current).toBe(99);
  expect(d.lastAppliedRingCursorRef.current).toBe(99);
  expect(d.applyStreamEventRef.current).not.toHaveBeenCalled();
});
