// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import Conversation from '../components/Conversation';
import { api } from '../lib/api';
import { withEndpointDiscovery } from './endpointFixture';

vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

async function mount(items: { id: string; text: string }[] = []) {
  vi.stubGlobal('fetch', withEndpointDiscovery(async (input) => {
    const path = String(input).split('?')[0];
    const payload = path === '/api/session/queue' ? { ok: true, session_id: 'queue-ui', items, recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [{ role: 'user', content: 'saved prior turn' }], display: [{ type: 'message', role: 'user', text: 'saved prior turn' }] }
      : path === '/api/session/state' ? { state: 'idle', runners: [], pending_swarms: false }
      : path === '/api/commands' ? { commands: [] }
      : path === '/api/swarm/live' ? []
      : path === '/api/workspace/files' ? { files: [], folders: [] } : {};
    return Response.json(payload);
  }));
  render(<Conversation config={null} activeSessionId="queue-ui" onArtifacts={() => {}} onJobChange={() => {}} />);
  await act(async () => { await Promise.resolve(); });
  return screen.getByPlaceholderText('Message the pilot...');
}

it('retains draft during pending and rejected save, blocking duplicate clicks', async () => {
  let rejectSave: (error: Error) => void = () => { throw new Error('no pending save'); };
  const save = vi.spyOn(api, 'queueAdd').mockImplementation(() => new Promise((_resolve, reject) => { rejectSave = reject; }));
  const input = await mount();
  fireEvent.change(input, { target: { value: 'keep my draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Queue', exact: true }));
  expect(input).toHaveValue('keep my draft');
  fireEvent.click(screen.getByRole('button', { name: 'Queue', exact: true }));
  expect(save).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledWith('keep my draft', [], 'queue-ui', { original_text: 'keep my draft', documents: [], retry_key: expect.any(String) });
  await act(async () => rejectSave(new Error('Prompt queue was not saved.')));
  expect(input).toHaveValue('keep my draft');
  expect(screen.getByText('Prompt queue was not saved.')).toBeTruthy();
});

it('keeps queued rows visible after clear or removal fails', async () => {
  vi.spyOn(api, 'queueClear').mockRejectedValue(new Error('Clear was not saved.'));
  vi.spyOn(api, 'queueRemove').mockRejectedValue(new Error('Removal was not saved.'));
  await mount([{ id: 'a', text: 'keep first' }, { id: 'b', text: 'keep second' }]);
  await screen.findByText('keep first');
  fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
  await screen.findByText('Clear was not saved.');
  expect(screen.getByText('keep first')).toBeTruthy();
  expect(screen.getByText('keep second')).toBeTruthy();
  fireEvent.click(screen.getAllByTitle('Remove from queue')[0]);
  await screen.findByText('Removal was not saved.');
  expect(screen.getByText('keep first')).toBeTruthy();
});

it('preserves text typed during a successful save', async () => {
  let complete: () => void = () => { throw new Error('no pending save'); };
  vi.spyOn(api, 'queueAdd').mockImplementation((text) => new Promise(resolve => {
    complete = () => resolve({ ok: true, item: { id: 'saved', text } });
  }));
  const input = await mount();
  fireEvent.change(input, { target: { value: 'first draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Queue', exact: true }));
  fireEvent.change(input, { target: { value: 'new draft while saving' } });
  await act(async () => complete());
  expect(input).toHaveValue('new draft while saving');
});

it('does not start the next turn when queue handoff fails', async () => {
  let finish: () => void = () => { throw new Error('no current stream'); };
  const chat = vi.spyOn(api, 'chat').mockImplementation((_message, onEvent, onDone) => {
    finish = () => { onEvent({ kind: 'assistant_done', data: {} }); onDone?.(); };
    return () => {};
  });
  const remove = vi.spyOn(api, 'queueHandoff').mockRejectedValue(new Error('Queue handoff failed.'));
  const input = await mount([{ id: 'later', text: 'must stay queued' }]);
  await screen.findByText('must stay queued');
  fireEvent.change(input, { target: { value: 'start current turn' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }));
  expect(chat).toHaveBeenCalledTimes(1);
  await act(async () => finish());
  await screen.findByText('Queue handoff failed.');
  expect(remove).toHaveBeenCalledWith('later', 'queue-ui');
  expect(chat).toHaveBeenCalledTimes(1);
  expect(screen.getByText('must stay queued')).toBeTruthy();
});


it('keyboard reorders the owned queue and retains rows when persistence fails', async () => {
  const reorder = vi.spyOn(api, 'queueReorder').mockRejectedValue(new Error('Queue reorder was not saved.'));
  await mount([{ id: 'a', text: 'first' }, { id: 'b', text: 'second' }]);
  const second = await screen.findByRole('button', { name: 'Edit queued prompt 2: second' });
  second.focus();
  expect(second).toHaveFocus();
  fireEvent.keyDown(second, { key: 'ArrowUp', altKey: true });
  await waitFor(() => expect(reorder).toHaveBeenCalledWith(['b', 'a'], 'queue-ui'));
  await screen.findByText('Queue reorder was not saved.');
  expect(screen.getByRole('button', { name: 'Edit queued prompt 1: first' })).toBeTruthy();
  expect(second).toHaveFocus();
});

it('clears deleted history and stops queue reads when the active session is explicitly cleared', async () => {
  vi.stubGlobal('fetch', withEndpointDiscovery(async (input) => {
    const path = String(input).split('?')[0];
    return Response.json(path === '/api/sessions/transcript'
      ? { history: [{ role: 'user', content: 'deleted session history' }], display: [{ type: 'message', role: 'user', text: 'deleted session history' }] }
      : path === '/api/session/state' ? { state: 'idle', runners: {}, pending_swarms: false }
      : path === '/api/swarm/live' ? [] : {});
  }));
  const list = vi.spyOn(api, 'queueList').mockResolvedValue({ ok: true, items: [], recovery: [], session_id: 'removed' });
  const props = { config: null, onArtifacts: () => {}, onJobChange: () => {} };
  const view = render(<Conversation {...props} activeSessionId="removed" />);
  await screen.findByText('deleted session history');
  const reads = list.mock.calls.length;
  view.rerender(<Conversation {...props} activeSessionId={null} />);
  await waitFor(() => expect(screen.queryByText('deleted session history')).toBeNull());
  expect(list).toHaveBeenCalledTimes(reads);
  expect(screen.queryByText('Active session changed. Queue refresh is pending.')).toBeNull();
});

it('does not paint a hop error when queueList returns another session id', async () => {
  vi.spyOn(api, 'queueList').mockResolvedValue({
    ok: true, items: [{ id: 'foreign', text: 'other session row' }], recovery: [], session_id: 'someone-else',
  });
  await mount();
  expect(screen.queryByText('Active session changed. Queue refresh is pending.')).toBeNull();
  expect(screen.queryByText('other session row')).toBeNull();
});

it('serializes queue discovery and pauses periodic reads while hidden', async () => {
  vi.useFakeTimers();
  let finish: (value: Awaited<ReturnType<typeof api.queueList>>) => void = () => {};
  const read = vi.spyOn(api, 'queueList').mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValue({ ok: true, session_id: 'queue-ui', items: [{ id: 'external', text: 'externally queued' }], recovery: [] });
  let hidden = false;
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
  try {
    await mount();
    await act(() => vi.advanceTimersByTimeAsync(12000));
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => finish({ ok: true, session_id: 'queue-ui', items: [], recovery: [] }));
    hidden = true;
    await act(() => vi.advanceTimersByTimeAsync(12000));
    expect(read).toHaveBeenCalledTimes(1);
    hidden = false;
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    expect(read).toHaveBeenCalledTimes(2);
    expect(screen.getByText('externally queued')).toBeTruthy();
  } finally { cleanup(); vi.useRealTimers(); }
});
