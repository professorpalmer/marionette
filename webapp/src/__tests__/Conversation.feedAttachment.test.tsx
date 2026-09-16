// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import Conversation from '../components/Conversation';
import { api } from '../lib/api';
import { clearTranscriptCache } from '../components/conversation/transcriptCache';
import { clearComposerDraftCache } from '../components/conversation/composerDraftCache';
import { withEndpointDiscovery } from './endpointFixture';

vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));

const observers: FeedObserver[] = [];
class FeedObserver {
  targets = new Set<Element>();
  constructor() { observers.push(this); }
  observe(target: Element) { this.targets.add(target); }
  unobserve(target: Element) { this.targets.delete(target); }
  disconnect() { this.targets.clear(); }
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  clearTranscriptCache();
  clearComposerDraftCache();
  observers.length = 0;
});

async function mount(initialSession: string | null) {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  vi.stubGlobal('ResizeObserver', FeedObserver);
  const added = vi.spyOn(HTMLElement.prototype, 'addEventListener');
  const removed = vi.spyOn(HTMLElement.prototype, 'removeEventListener');
  let activeSession = initialSession;
  vi.spyOn(api, 'readEventsSince').mockImplementation(async () => ({ session_id: activeSession ?? '', cursor: 0, events: [] }));
  vi.stubGlobal('fetch', withEndpointDiscovery(async input => {
    const path = String(input).split('?')[0];
    const payload = path === '/api/session/queue' ? { ok: true, session_id: activeSession, items: [], recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [{ role: 'user', content: 'prior turn' }], display: [{ type: 'message', role: 'user', text: 'prior turn' }] }
      : path === '/api/session/state' ? { state: 'idle', runners: {}, pending_swarms: false }
      : path === '/api/context/usage' ? { available: true, session_id: activeSession, total: 0, limit: 10000, categories: [] }
      : path === '/api/commands' ? { commands: [] }
      : path === '/api/swarm/live' ? []
      : path === '/api/workspace/files' ? { files: [], folders: [] } : {};
    return Response.json(payload);
  }));
  const props = { config: null, onArtifacts: () => {}, onJobChange: () => {} };
  const view = render(<Conversation {...props} activeSessionId={activeSession} />);
  await act(async () => { await Promise.resolve(); });
  return {
    added, removed,
    switchTo: async (id: string) => {
      activeSession = id;
      view.rerender(<Conversation {...props} activeSessionId={id} />);
      await act(async () => { await Promise.resolve(); });
    },
  };
}

it.each([null, 'feed-A'])('moves the growth observer from %s through B/A/B', async initial => {
  const fixture = await mount(initial);
  for (const id of ['feed-B', 'feed-A', 'feed-B']) {
    const oldContent = screen.getByTestId('transcript-feed-content');
    const oldObserver = observers.find(observer => observer.targets.has(oldContent));
    expect(oldObserver).toBeDefined();
    await fixture.switchTo(id);
    const content = screen.getByTestId('transcript-feed-content');
    const viewport = screen.getByTestId('transcript-feed-scrollport');
    expect(content).not.toBe(oldContent);
    expect(oldObserver?.targets.size).toBe(0);
    expect(observers.some(observer => observer.targets.has(content) && observer.targets.has(viewport))).toBe(true);
  }
});

it.each([null, 'feed-A'])('moves manual scroll listeners from %s through B/A/B', async initial => {
  const fixture = await mount(initial);
  for (const id of ['feed-B', 'feed-A', 'feed-B']) {
    const oldViewport = screen.getByTestId('transcript-feed-scrollport');
    const oldListeners = fixture.added.mock.calls.flatMap((call, index) =>
      fixture.added.mock.contexts[index] === oldViewport ? [call] : []);
    // Start an idle timer on the outgoing pane; switching must cancel it.
    const scheduled = vi.spyOn(window, 'setTimeout');
    fireEvent.wheel(oldViewport, { deltaY: -10 });
    const timerIndex = scheduled.mock.calls.findLastIndex(call => call[1] === 150);
    expect(timerIndex).toBeGreaterThanOrEqual(0);
    const timer = scheduled.mock.results[timerIndex]?.value;
    const clearTimer = vi.spyOn(window, 'clearTimeout');
    await fixture.switchTo(id);
    expect(clearTimer).toHaveBeenCalledWith(timer);
    scheduled.mockRestore();
    clearTimer.mockRestore();
    const viewport = screen.getByTestId('transcript-feed-scrollport');
    for (const type of ['wheel', 'scroll', 'touchstart', 'touchmove', 'touchend', 'touchcancel', 'keydown']) {
      const oldCalls = oldListeners.filter(call => call[0] === type);
      expect(oldCalls.length).toBeGreaterThan(0);
      for (const call of oldCalls) {
        expect(fixture.removed.mock.calls.some((removed, index) =>
          fixture.removed.mock.contexts[index] === oldViewport && removed[0] === type && removed[1] === call[1])).toBe(true);
      }
      expect(fixture.added.mock.calls.some((call, index) =>
        fixture.added.mock.contexts[index] === viewport && call[0] === type)).toBe(true);
    }
  }
});
