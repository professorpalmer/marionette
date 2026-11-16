// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import Conversation from '../components/Conversation';
import { api } from '../lib/api';
import { withEndpointDiscovery } from './endpointFixture';
import { clearComposerAttachmentCache } from '../components/conversation/composerAttachmentCache';
import { clearComposerDraftCache } from '../components/conversation/composerDraftCache';
import { clearTerminalSelectionCache } from '../components/conversation/terminalSelectionCache';

vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));
afterEach(() => {
  cleanup(); clearTerminalSelectionCache(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  localStorage.clear(); clearComposerAttachmentCache(); clearComposerDraftCache();
});

const EOF_COPY = 'Provider stream ended before a clean finish.';

async function mountAndSend() {
  vi.stubGlobal('fetch', withEndpointDiscovery(async (input) => {
    const path = String(input).split('?')[0];
    const payload = path === '/api/session/queue' ? { ok: true, session_id: 'A', items: [], held_items: [], receipts: [], recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [], display: [] }
      : path === '/api/session/state' ? { state: 'idle', runners: [], pending_swarms: false }
      : path === '/api/commands' ? { commands: [] }
      : path === '/api/swarm/live' ? []
      : path === '/api/workspace/files' ? { files: [], folders: [] } : {};
    return Response.json(payload);
  }));
  let event: Parameters<typeof api.chat>[1] = () => {};
  let done: () => void = () => {};
  vi.spyOn(api, 'chat').mockImplementation((_message, onEvent, onDone) => {
    event = onEvent; done = () => onDone?.(); return () => {};
  });
  render(<Conversation config={null} onArtifacts={() => {}} onJobChange={() => {}} activeSessionId="A" />);
  await act(async () => { await Promise.resolve(); });
  fireEvent.change(screen.getByPlaceholderText('Message the pilot...'), { target: { value: 'go' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }));
  await act(async () => {
    event({ kind: 'message_delta', data: { text: 'Working on the fix' }, cursor: 10, generation: 3 });
  });
  return { close: () => act(async () => { done(); await Promise.resolve(); }) };
}

it('settles from the ring terminal when the live stream closes without one', async () => {
  const replay = vi.spyOn(api, 'chatEvents').mockResolvedValue({
    ok: true, session_id: 'A', generation: 3, cursor: 12, missed: false, available: true,
    events: [
      { cursor: 11, kind: 'assistant_done', data: { stop_cause: 'natural', finish_reason: 'end_turn' } },
      { cursor: 12, kind: 'done', data: {} },
    ],
  } as unknown as Awaited<ReturnType<typeof api.chatEvents>>);
  const stream = await mountAndSend();
  await stream.close();
  await waitFor(() => expect(replay).toHaveBeenCalledWith({ session: 'A', since: 10, generation: 3 }));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 20)); });
  expect(screen.queryByText(EOF_COPY)).toBeNull();
});

it('paints provider EOF when the ring holds no terminal either', async () => {
  vi.spyOn(api, 'chatEvents').mockResolvedValue({
    ok: true, session_id: 'A', generation: 3, cursor: 10, missed: false, available: true, events: [],
  } as unknown as Awaited<ReturnType<typeof api.chatEvents>>);
  const stream = await mountAndSend();
  await stream.close();
  expect(await screen.findByText(EOF_COPY)).toBeTruthy();
});
