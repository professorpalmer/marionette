// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { putTerminalSelection, clearTerminalSelectionCache } from '../components/conversation/terminalSelectionCache';
import Conversation from '../components/Conversation';
import { api, type InputReceipt, type ServerQueueItem } from '../lib/api';
import { withEndpointDiscovery } from './endpointFixture';
import { clearComposerAttachmentCache } from '../components/conversation/composerAttachmentCache';
import { clearComposerDraftCache } from '../components/conversation/composerDraftCache';

vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));
afterEach(() => { cleanup(); clearTerminalSelectionCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); clearComposerAttachmentCache(); clearComposerDraftCache(); });
const original: InputReceipt = {
  id: 'receipt', original_text: '  original\nwith spacing  ', attachments: [
    { ref: 'input:A:image', kind: 'image', name: 'photo.png', byte_length: 10, sha256: 'image' },
    { ref: 'input:A:document', kind: 'document', name: 'notes.txt', byte_length: 20, sha256: 'document' },
  ], model: 'stamped', payload_digest: 'payload', created_at: 1, status: 'uncertain', reason: 'interrupted', held: true,
};
async function mount(items: ServerQueueItem[] = [], receipts: InputReceipt[] = []) {
  vi.stubGlobal('fetch', withEndpointDiscovery(async (input) => {
    const path = String(input).split('?')[0];
    const payload = path === '/api/session/queue' ? { ok: true, session_id: 'A', items, held_items: [{ id: 'held', text: 'never drain' }], receipts, recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [{ role: 'user', content: 'prior' }], display: [{ type: 'message', role: 'user', text: 'prior' }] }
      : path === '/api/session/state' ? { state: 'idle', runners: [], pending_swarms: false }
      : path === '/api/commands' ? { commands: [] }
      : path === '/api/swarm/live' ? []
      : path === '/api/workspace/files' ? { files: [], folders: [] } : {};
    return Response.json(payload);
  }));
  const props = { config: null, onArtifacts: () => {}, onJobChange: () => {} };
  const view = render(<Conversation {...props} activeSessionId="A" />);
  await act(async () => { await Promise.resolve(); });
  const input = screen.getByPlaceholderText('Message the pilot...');
  return { input, switchTo: async (id: string) => { view.rerender(<Conversation {...props} activeSessionId={id} />); await act(async () => { await Promise.resolve(); }); } };
}
function queue() { fireEvent.click(screen.getByRole('button', { name: 'Queue', exact: true })); }
async function send() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }));
}
async function copy() {
  const summary = await screen.findByText(/Saved inputs/);
  fireEvent.click(summary);
  fireEvent.click(await screen.findByText(/Delivery uncertain.*held for review/));
  fireEvent.click(await screen.findByRole('button', { name: 'Copy original to draft' }));
}
function streamMock() {
  let event: Parameters<typeof api.chat>[1] = () => {};
  let done: () => void = () => {};
  let error: (err: Error) => void = () => {};
  const chat = vi.spyOn(api, 'chat').mockImplementation((_message, onEvent, onDone, onError) => {
    event = onEvent; done = () => onDone?.(); error = err => onError?.(err); return () => {};
  });
  return {
    chat,
    accept: () => act(async () => {
      const last = chat.mock.calls.at(-1);
      const submittedId = String(last?.[6]?.input_id || '').trim() || 'new';
      event({ kind: 'input_receipt', data: { input_id: submittedId, status: 'accepted' } });
    }),
    finish: () => act(async () => { event({ kind: 'assistant_done', data: {} }); done(); }),
    fail: () => act(async () => error(new Error('transport interrupted'))),
  };
}

it('shows held originals after reload and copies exact text, images and documents locally without draining', async () => {
  const handoff = vi.spyOn(api, 'queueHandoff');
  const save = vi.spyOn(api, 'queueAdd').mockRejectedValue(new Error('keep draft'));
  const stream = streamMock();
  const { input } = await mount([], [original]);
  expect(await screen.findByText(/Saved inputs/)).toBeTruthy();
  fireEvent.change(input, { target: { value: 'existing' } });
  await copy();
  expect(input).toHaveValue('existing\n\n' + original.original_text);
  expect(screen.getByRole('button', { name: 'Remove document notes.txt' })).toBeTruthy();
  expect(screen.getAllByAltText('photo.png')).toHaveLength(2);
  expect(handoff).not.toHaveBeenCalled(); expect(stream.chat).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled();
  queue();
  await waitFor(() => expect(save).toHaveBeenCalledWith('existing\n\n' + original.original_text, ['input:A:image'], 'A', {
    documents: [{ ref: 'input:A:document', name: 'notes.txt' }], retry_key: expect.any(String), original_text: 'existing\n\n' + original.original_text,
  }));
  expect(input).toHaveValue('existing\n\n' + original.original_text);
});

it('refuses image overflow before changing the draft or removing a queued original', async () => {
  const many = { ...original, attachments: Array.from({ length: 8 }, (_, i) => ({ ...original.attachments[0], ref: `input:A:${i}` })) };
  const remove = vi.spyOn(api, 'queueRemove');
  const { input } = await mount([{ id: original.id, text: 'converted' }], [many]);
  await copy();
  const before = original.original_text;
  fireEvent.click(screen.getByRole('button', { name: 'Edit queued prompt 1: converted' }));
  expect(screen.getByText(/Maximum 8 images per message/)).toBeTruthy();
  expect(input).toHaveValue(before); expect(remove).not.toHaveBeenCalled();
});

it('queue edit keeps the receipt original, retained attachments and existing draft on removal failure', async () => {
  vi.spyOn(api, 'queueRemove').mockRejectedValue(new Error('removal refused'));
  const { input } = await mount([{ id: original.id, text: 'converted' }], [original]);
  fireEvent.change(input, { target: { value: 'existing' } });
  fireEvent.click(await screen.findByRole('button', { name: 'Edit queued prompt 1: converted' }));
  await screen.findByText('removal refused');
  expect(input).toHaveValue('existing\n\n' + original.original_text);
  expect(screen.getByRole('button', { name: 'Remove document notes.txt' })).toBeTruthy();
});

it('keeps an uncertain submission retry key and gives changed payloads a new identity', async () => {
  const save = vi.spyOn(api, 'queueAdd').mockRejectedValue(new Error('uncertain admission'));
  const { input } = await mount();
  fireEvent.change(input, { target: { value: '  exact\ntext  ' } });
  queue(); await screen.findByText('uncertain admission');
  const first = save.mock.calls[0][3]?.retry_key;
  queue(); await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
  expect(save.mock.calls[1][3]?.retry_key).toBe(first);
  await act(async () => { await Promise.resolve(); });
  fireEvent.change(input, { target: { value: '  exact\ntext changed  ' } });
  queue(); await waitFor(() => expect(save).toHaveBeenCalledTimes(3));
  expect(save.mock.calls[2][3]?.retry_key).not.toBe(first);
  expect(save.mock.calls[0][0]).toBe('  exact\ntext  ');
});

it('normal send retains text and attachments until explicit admission, preserving newer edits', async () => {
  const stream = streamMock();
  const { input } = await mount([], [original]);
  await copy(); await send();
  expect(stream.chat.mock.calls[0][0]).toBe(original.original_text);
  expect(stream.chat.mock.calls[0][5]).toEqual(['input:A:image']);
  expect(stream.chat.mock.calls[0][6]?.documents).toEqual([{ ref: 'input:A:document', name: 'notes.txt' }]);
  expect(input).toHaveValue(original.original_text);
  fireEvent.change(input, { target: { value: 'new draft' } });
  await stream.accept();
  expect(input).toHaveValue('new draft');
  expect(screen.queryByRole('button', { name: 'Remove document notes.txt' })).toBeNull();
});

it('unaccepted stream failure retains the exact draft and key for explicit retry', async () => {
  const stream = streamMock(); const { input } = await mount();
  fireEvent.change(input, { target: { value: '  retry me  ' } }); await send();
  const first = stream.chat.mock.calls[0][6]?.retry_key;
  await stream.fail(); expect(input).toHaveValue('  retry me  ');
  await send(); expect(stream.chat.mock.calls[1][6]?.retry_key).toBe(first);
});

it('restores A attachments after a switch and ignores late A acceptance in B', async () => {
  const stream = streamMock(); const { input, switchTo } = await mount([], [original]);
  await copy(); await send(); await switchTo('B');
  fireEvent.change(input, { target: { value: 'B draft' } }); await stream.accept();
  expect(input).toHaveValue('B draft');
  expect(screen.queryByRole('button', { name: 'Remove document notes.txt' })).toBeNull();
  await switchTo('A'); expect(input).toHaveValue(original.original_text);
  expect(screen.getByRole('button', { name: 'Remove document notes.txt' })).toBeTruthy();
});

it('swaps the stamped model before handing off and passes the exact one-use identity to chat', async () => {
  const order: string[] = [];
  vi.spyOn(api, 'swapPilot').mockImplementation(async () => { order.push('swap'); return { ok: true }; });
  const handoff = vi.spyOn(api, 'queueHandoff').mockImplementation(async () => { order.push('handoff'); return { ok: true, item: { id: 'next', input_id: 'exact-id', handoff_token: 'once', text: 'queued' } }; });
  const remove = vi.spyOn(api, 'queueRemove'); const stream = streamMock();
  const { input } = await mount([{ id: 'next', text: 'queued', model: 'stamped', documents: ['input:A:document'] }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await waitFor(() => expect(stream.chat).toHaveBeenCalledTimes(2));
  expect(order).toEqual(['swap', 'handoff']); expect(handoff).toHaveBeenCalledWith('next', 'A'); expect(remove).not.toHaveBeenCalled();
  expect(stream.chat.mock.calls[1][6]).toEqual({ session_id: 'A', input_id: 'exact-id', handoff_token: 'once', documents: [{ ref: 'input:A:document' }] });
});

it('does not consume or deliver a queue entry when its model swap fails', async () => {
  vi.spyOn(api, 'swapPilot').mockRejectedValue(new Error('model unavailable'));
  const handoff = vi.spyOn(api, 'queueHandoff'); const stream = streamMock();
  const { input } = await mount([{ id: 'next', text: 'queued', model: 'stamped' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await screen.findByText('model unavailable'); expect(handoff).not.toHaveBeenCalled(); expect(stream.chat).toHaveBeenCalledTimes(1);
});

it('does not deliver a late handoff after switching sessions', async () => {
  let resolve: (value: Awaited<ReturnType<typeof api.queueHandoff>>) => void = () => {};
  const handoff = vi.spyOn(api, 'queueHandoff').mockImplementation(() => new Promise(r => { resolve = r; }));
  const stream = streamMock(); const { input, switchTo } = await mount([{ id: 'next', text: 'queued' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await waitFor(() => expect(handoff).toHaveBeenCalled()); await switchTo('B');
  await act(async () => resolve({ ok: true, item: { id: 'next', input_id: 'next', handoff_token: 'once', text: 'queued' } }));
  expect(stream.chat).toHaveBeenCalledTimes(1);
});

it('preserves the matching draft through stream close without treating EOF as admission', async () => {
  let close: () => void = () => {};
  vi.spyOn(api, 'chat').mockImplementation((_message, _event, done) => { close = () => done?.(); return () => {}; });
  const { input } = await mount(); fireEvent.change(input, { target: { value: '  unacknowledged  ' } }); await send();
  await act(async () => close()); expect(input).toHaveValue('  unacknowledged  ');
});

it('does not consume a queued original when a new same-session stream starts during model swap', async () => {
  let complete: () => void = () => {};
  const swap = vi.spyOn(api, 'swapPilot').mockImplementation(() => new Promise(resolve => { complete = () => resolve({ ok: true }); }));
  const handoff = vi.spyOn(api, 'queueHandoff'); const stream = streamMock();
  const { input } = await mount([{ id: 'next', text: 'queued', model: 'stamped' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await waitFor(() => expect(swap).toHaveBeenCalled());
  // A new current stream invalidates the idle-drain attempt even in the same session.
  fireEvent.change(input, { target: { value: 'new current turn' } }); await send();
  await act(async () => complete()); expect(handoff).not.toHaveBeenCalled(); expect(stream.chat).toHaveBeenCalledTimes(2);
});

it('does not let a late queue admission clear an A draft after A-B-A switching', async () => {
  let complete: () => void = () => {};
  vi.spyOn(api, 'queueAdd').mockImplementation(text => new Promise(resolve => { complete = () => resolve({ ok: true, item: { id: 'saved', text } }); }));
  const { input, switchTo } = await mount(); fireEvent.change(input, { target: { value: 'A original' } }); queue();
  await switchTo('B'); fireEvent.change(input, { target: { value: 'B draft' } }); await switchTo('A');
  await act(async () => complete()); expect(input).toHaveValue('A original');
});

it('busy steer sends exact originals and documents, retaining them when admission fails', async () => {
  const stream = streamMock();
  const steer = vi.spyOn(api, 'steerSession').mockRejectedValue(new Error('input admission refused'));
  const { input } = await mount([], [original]);
  fireEvent.change(input, { target: { value: 'start turn' } }); await send(); await stream.accept();
  await copy();
  fireEvent.keyDown(input, { key: 'Enter', altKey: true });
  await waitFor(() => expect(steer).toHaveBeenCalled());
  expect(steer.mock.calls[0][0]).toBe(original.original_text);
  expect(steer.mock.calls[0][1]).toEqual(['input:A:image']);
  expect(steer.mock.calls[0][3]).toMatchObject({ sessionId: 'A', documents: [{ ref: 'input:A:document', name: 'notes.txt' }], retry_key: expect.any(String) });
  expect(input).toHaveValue(original.original_text);
  expect(screen.getByRole('button', { name: 'Remove document notes.txt' })).toBeTruthy();
});

it('an uncertain handoff is never requested again automatically after another turn finishes', async () => {
  const handoff = vi.spyOn(api, 'queueHandoff').mockRejectedValue(new Error('handoff response lost'));
  const stream = streamMock(); const { input } = await mount([{ id: 'next', text: 'queued' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await screen.findByText('handoff response lost'); expect(handoff).toHaveBeenCalledTimes(1);
  fireEvent.change(input, { target: { value: 'another explicit turn' } }); await send(); await stream.finish();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 90)); });
  expect(handoff).toHaveBeenCalledTimes(1);
});

it('Stop and a same-session replacement turn invalidate a pending handoff', async () => {
  let complete: () => void = () => {};
  const handoff = vi.spyOn(api, 'queueHandoff').mockImplementation(() => new Promise(resolve => { complete = () => resolve({ ok: true, item: { id: 'next', text: 'queued', input_id: 'next', handoff_token: 'once' } }); }));
  vi.spyOn(api, 'interruptSession').mockResolvedValue({ ok: true });
  const stream = streamMock(); const { input } = await mount([{ id: 'next', text: 'queued' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send(); await stream.finish();
  await waitFor(() => expect(handoff).toHaveBeenCalled());
  fireEvent.change(input, { target: { value: 'replacement' } }); await send();
  fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  await act(async () => complete()); expect(stream.chat).toHaveBeenCalledTimes(2);
});

it.each([false, true])('custom rendering preserves newer drafts, including session switch=%s', async switchSession => {
  vi.spyOn(api, 'listCommands').mockResolvedValue({ commands: [{ name: 'example', description: 'Example', scope: 'repo' }] });
  let complete: () => void = () => {};
  const renderCommand = vi.spyOn(api, 'renderCommand').mockImplementation(() => new Promise(resolve => { complete = () => resolve({ name: 'example', prompt: 'rendered prompt' }); }));
  const { input, switchTo } = await mount();
  fireEvent.change(input, { target: { value: '  /example args  ' } }); await send();
  await waitFor(() => expect(renderCommand).toHaveBeenCalledWith('example', 'args'));
  if (switchSession) await switchTo('B');
  fireEvent.change(input, { target: { value: 'newer draft' } }); await act(async () => complete());
  expect(input).toHaveValue('newer draft');
});

it('queues literal tokens and changes retry identity when either representation changes', async () => {
  const save = vi.spyOn(api, 'queueAdd').mockRejectedValue(new Error('uncertain expansion'));
  const { input } = await mount();
  const raw = '  inspect @terminal:"zsh:1"\n\t';
  putTerminalSelection('A', 'zsh:1', 'first output');
  fireEvent.change(input, { target: { value: raw } });
  queue(); await screen.findByText('uncertain expansion');
  expect(save.mock.calls[0][0]).toBe('  inspect ```terminal\nfirst output\n```\n\t');
  expect(save.mock.calls[0][3]?.original_text).toBe(raw);
  queue(); await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
  expect(save.mock.calls[1][3]?.retry_key).toBe(save.mock.calls[0][3]?.retry_key);
  await act(async () => { await Promise.resolve(); });
  putTerminalSelection('A', 'zsh:1', 'second output');
  queue(); await waitFor(() => expect(save).toHaveBeenCalledTimes(3));
  expect(save.mock.calls[2][3]?.retry_key).not.toBe(save.mock.calls[1][3]?.retry_key);
  await act(async () => { await Promise.resolve(); });
  fireEvent.change(input, { target: { value: raw.replace('"zsh:1"', 'zsh:1') } });
  queue(); await waitFor(() => expect(save).toHaveBeenCalledTimes(4));
  expect(save.mock.calls[3][0]).toBe(save.mock.calls[2][0]);
  expect(save.mock.calls[3][3]?.retry_key).not.toBe(save.mock.calls[2][3]?.retry_key);
});

it('sends literal terminal draft separately and Copy never expands or submits it', async () => {
  const stream = streamMock();
  const raw = '  inspect @terminal:"zsh:1"\n\t';
  const receipt = { ...original, original_text: raw, delivery_text: 'old frozen output' };
  const { input } = await mount([], [receipt]);
  putTerminalSelection('A', 'zsh:1', 'current output');
  await copy();
  expect(input).toHaveValue(raw);
  expect(stream.chat).not.toHaveBeenCalled();
  await send();
  expect(stream.chat.mock.calls[0][0]).toBe('  inspect ```terminal\ncurrent output\n```\n\t');
  expect(stream.chat.mock.calls[0][6]?.original_text).toBe(raw);
  expect(stream.chat.mock.calls[0][6]?.documents).toEqual([{ ref: 'input:A:document', name: 'notes.txt' }]);
});

it('busy steer carries the literal draft and the current frozen terminal expansion', async () => {
  const stream = streamMock();
  const steer = vi.spyOn(api, 'steerSession').mockRejectedValue(new Error('keep steer'));
  const { input } = await mount();
  fireEvent.change(input, { target: { value: 'start turn' } }); await send(); await stream.accept();
  const raw = '  inspect @terminal:"zsh:1"\n\t';
  putTerminalSelection('A', 'zsh:1', 'steer output');
  fireEvent.change(input, { target: { value: raw } });
  fireEvent.keyDown(input, { key: 'Enter', altKey: true });
  await waitFor(() => expect(steer).toHaveBeenCalled());
  expect(steer.mock.calls[0][0]).toBe('  inspect ```terminal\nsteer output\n```\n\t');
  expect(steer.mock.calls[0][3]).toMatchObject({ sessionId: 'A', original_text: raw, retry_key: expect.any(String) });
  expect(input).toHaveValue(raw);
});

it('auto carries exact literal text and expanded delivery separately', async () => {
  const auto = vi.spyOn(api, 'auto').mockReturnValue(() => {});
  const { input } = await mount();
  const raw = '  inspect @terminal:"zsh:1"\n\t';
  putTerminalSelection('A', 'zsh:1', 'auto output');
  fireEvent.change(input, { target: { value: raw } });
  fireEvent.click(screen.getByTitle('Autopilot: the pilot plans and executes autonomously (vs. you steering each step)'));
  fireEvent.click(screen.getByRole('button', { name: 'Run', exact: true }));
  await waitFor(() => expect(auto).toHaveBeenCalled());
  expect(auto.mock.calls[0][0]).toBe('  inspect ```terminal\nauto output\n```\n\t');
  expect(auto.mock.calls[0][5]).toMatchObject({ session_id: 'A', original_text: raw, retry_key: expect.any(String) });
  expect(input).toHaveValue(raw);
});

it.each(["thinking", "idle", "awaiting_swarm"] as const)("reconciles failed Stop against authoritative %s state", async state => {
  const stream = streamMock();
  const handoff = vi.spyOn(api, 'queueHandoff');
  const { input } = await mount([{ id: 'queued', text: 'must stay queued' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send();
  vi.spyOn(api, 'interruptSession').mockRejectedValue(new Error('transport failed'));
  const read = vi.spyOn(api, 'getSessionState').mockResolvedValue({ state, pending_swarms: false, runners: { A: state === 'idle' ? 'idle' : 'running' } });
  fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  await waitFor(() => expect(read).toHaveBeenCalledWith({ sessionId: 'A' }));
  if (state === 'thinking') expect(screen.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled();
  else expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 90)); });
  expect(handoff).not.toHaveBeenCalled();
  expect(stream.chat).toHaveBeenCalledTimes(1);
});

it.each(['switch', 'roundtrip', 'replacement'])("ignores delayed failed-Stop state after %s", async change => {
  streamMock();
  const { input, switchTo } = await mount();
  fireEvent.change(input, { target: { value: 'start' } }); await send();
  vi.spyOn(api, 'interruptSession').mockResolvedValue({ ok: false });
  let resolve: (state: Awaited<ReturnType<typeof api.getSessionState>>) => void = () => {};
  const read = vi.spyOn(api, 'getSessionState').mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  await waitFor(() => expect(read).toHaveBeenCalled());
  if (change === 'replacement') {
    fireEvent.change(input, { target: { value: 'replacement' } }); await send();
    vi.mocked(api.interruptSession).mockResolvedValue({ ok: true });
    fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  } else {
    await switchTo('B');
    if (change === 'roundtrip') await switchTo('A');
  }
  await act(async () => resolve({ state: 'thinking', pending_swarms: false, runners: { A: 'running' } }));
  expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
});

it("does not replay a rendered partial assistant frame after failed Stop recovery", async () => {
  vi.spyOn(api, 'getSessionState').mockResolvedValue({ state: 'thinking', pending_swarms: false, runners: { A: 'running' } });
  vi.spyOn(api, 'interruptSession').mockResolvedValue({ ok: false });
  const live = vi.spyOn(api, 'chatEventsLive').mockReturnValue(() => {});
  await mount();
  await waitFor(() => expect(live).toHaveBeenCalled());
  const originalEvent = live.mock.calls.at(-1)?.[1];
  await act(async () => {
    originalEvent?.({ kind: 'message_delta', data: { text: 'Already rendered.' }, cursor: 7 });
  });
  await screen.findByText('Already rendered.');
  live.mockClear();
  fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  await waitFor(() => expect(live).toHaveBeenCalledTimes(1));
  const recovered = live.mock.calls[0];
  await act(async () => {
    if ((recovered[0].since ?? 0) < 7) {
      recovered[1]({ kind: 'message_delta', data: { text: 'Already rendered.' }, cursor: 7 });
    }
    originalEvent?.({ kind: 'message_delta', data: { text: 'Stale callback.' }, cursor: 8 });
    recovered[1]({ kind: 'message_delta', data: { text: ' Continuing.' }, cursor: 8 });
    recovered[1]({ kind: 'assistant_done', data: {}, cursor: 9 });
  });
  expect(document.body.textContent?.match(/Already rendered\./g)).toHaveLength(1);
  expect(document.body.textContent).toContain('Continuing.');
  expect(document.body.textContent).not.toContain('Stale callback.');
  expect(recovered[0]).toMatchObject({ session: 'A', since: 7 });
  expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
});

it("observes terminal recovery after failed Stop without draining or resuming queued work", async () => {
  const stream = streamMock();
  const handoff = vi.spyOn(api, 'queueHandoff');
  const live = vi.spyOn(api, 'chatEventsLive').mockReturnValue(() => {});
  const { input } = await mount([{ id: 'queued', text: 'stay queued' }]);
  fireEvent.change(input, { target: { value: 'start' } }); await send();
  vi.spyOn(api, 'interruptSession').mockResolvedValue({ ok: false });
  vi.spyOn(api, 'getSessionState').mockResolvedValue({ state: 'thinking', pending_swarms: false, runners: { A: 'running' } });
  live.mockClear();
  fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }));
  await waitFor(() => expect(live).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled();
  expect(screen.queryByTestId('turn-terminal-chip')).toBeNull();
  const onEvent = live.mock.calls.at(-1)?.[1];
  await act(async () => {
    onEvent?.({ kind: 'pilot_resume', data: {} });
    onEvent?.({ kind: 'assistant_done', data: { stop_cause: 'natural' } });
    await new Promise(resolve => setTimeout(resolve, 100));
  });
  expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
  expect(handoff).not.toHaveBeenCalled(); expect(stream.chat).toHaveBeenCalledTimes(1);
});
