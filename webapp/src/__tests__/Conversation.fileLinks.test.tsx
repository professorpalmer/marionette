// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import Conversation from '../components/Conversation';
import { api } from '../lib/api';
import { nativeFs } from '../lib/transport';
import { openAgentFile } from '../lib/agentLinks';
import { clearTranscriptCache } from '../components/conversation/transcriptCache';
import { clearComposerDraftCache } from '../components/conversation/composerDraftCache';
import { withEndpointDiscovery } from './endpointFixture';
vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));
vi.mock('../components/FileEditorPane', () => ({ default: ({ path, line, col }: { path: string; line?: number; col?: number }) => <div data-testid="editor">{path}:{line}:{col}</div> }));
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear();
  clearTranscriptCache(); clearComposerDraftCache();
});
async function mount() {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  vi.spyOn(api, 'readEventsSince').mockResolvedValue({ session_id: 'files-A', cursor: 0, events: [] });
  vi.stubGlobal('fetch', withEndpointDiscovery(async input => {
    const path = String(input).split('?')[0];
    return Response.json(path === '/api/session/queue' ? { ok: true, session_id: 'files-A', items: [], recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [{ role: 'assistant', content: '[Open fixture](file:///tmp/space%20%2520.txt:12:3)' }], display: [{ type: 'message', role: 'assistant', text: '[Open fixture](file:///tmp/space%20%2520.txt:12:3)' }] }
      : path === '/api/session/state' ? { state: 'idle', runners: {}, pending_swarms: false }
      : path === '/api/context/usage' ? { available: true, session_id: 'files-A', total: 0, limit: 10000, categories: [] }
      : path === '/api/commands' ? { commands: [] }
      : path === '/api/swarm/live' ? [] : {});
  }));
  render(<Conversation config={null} onArtifacts={() => {}} onJobChange={() => {}} activeSessionId="files-A" />);
  await act(async () => { await Promise.resolve(); });
}
it('routes an actual local-file click to native open once without creating an editor', async () => {
  vi.spyOn(api, 'resolveFile').mockRejectedValue(new Error('403'));
  const open = vi.spyOn(nativeFs, 'openPath').mockResolvedValue({ ok: true, action: 'opened' });
  await mount();
  fireEvent.click(await screen.findByText('Open fixture'));
  await waitFor(() => expect(open).toHaveBeenCalledExactlyOnceWith('/tmp/space %20.txt'));
  expect(screen.queryByTestId('editor')).toBeNull();
});
it('keeps a unique workspace match in the editor with line and column', async () => {
  vi.spyOn(api, 'resolveFile').mockResolvedValue({ ok: true, path: 'src/a.ts' });
  const open = vi.spyOn(nativeFs, 'openPath');
  await mount();
  await act(async () => openAgentFile('src/a.ts:12:3'));
  await waitFor(() => expect(screen.getByTestId('editor').textContent).toBe('src/a.ts:12:3'));
  expect(open).not.toHaveBeenCalled();
});
it('does not fall back for ambiguous or missing relative references', async () => {
  const resolve = vi.spyOn(api, 'resolveFile').mockResolvedValue({ ok: false, candidates: ['a/x.ts', 'b/x.ts'] });
  const open = vi.spyOn(nativeFs, 'openPath');
  await mount();
  await act(async () => openAgentFile('/tmp/x.ts'));
  resolve.mockRejectedValue(new Error('404'));
  await act(async () => openAgentFile('src/missing.ts'));
  expect(open).not.toHaveBeenCalled();
  expect(screen.queryByTestId('editor')).toBeNull();
});
it('surfaces native failures and missing desktop support', async () => {
  vi.spyOn(api, 'resolveFile').mockRejectedValue(new Error('403'));
  const messages: unknown[] = [];
  const listener = (event: Event) => { if (event instanceof CustomEvent) messages.push(event.detail); };
  window.addEventListener('harness-toast', listener);
  try {
    await mount();
    await act(async () => openAgentFile('~/Downloads/fixture.txt'));
    expect(messages.some(message => typeof message === 'string' && message.includes('desktop app'))).toBe(true);
    vi.spyOn(nativeFs, 'openPath').mockResolvedValue({ ok: false, error: 'No application associated' });
    await act(async () => openAgentFile('/tmp/fixture.txt'));
    expect(messages).toContain('No application associated');
    expect(screen.queryByTestId('editor')).toBeNull();
  } finally { window.removeEventListener('harness-toast', listener); }
});
