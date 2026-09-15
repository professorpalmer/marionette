// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import Conversation from '../components/Conversation';
import { api } from '../lib/api';
import { clearTranscriptCache } from '../components/conversation/transcriptCache';
import { clearComposerDraftCache } from '../components/conversation/composerDraftCache';
import { withEndpointDiscovery } from './endpointFixture';
import { getActiveDiagnostic, resetDiagnosticBus } from '../lib/operationalDiagnosticBus';

vi.mock('../components/PilotPicker', () => ({ default: () => null }));
vi.mock('../components/SwarmReasoningPicker', () => ({ default: () => null }));
vi.mock('../components/conversation/WorkspaceChip', () => ({ default: () => null }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  clearTranscriptCache();
  clearComposerDraftCache();
  resetDiagnosticBus();
});

const jobId = 'local-command-lifecycle';
function receipt(status: 'running' | 'completed', id = jobId) {
  return {
    kind: 'action_result',
    data: {
      id: `action-${id}`, kind: 'run_command', job_id: id, status,
      command: 'echo lifecycle',
      ...(status === 'completed' ? { exit_code: 0, output: 'command finished', terminal_receipt: true } : {}),
    },
  };
}

async function mount(display: unknown[] = [{ type: 'message', role: 'user', text: 'prior turn' }]) {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  let event: Parameters<typeof api.chat>[1] = () => { throw new Error('chat not started'); };
  let finishPoll: (result: Awaited<ReturnType<typeof api.getSwarmResults>>) => void = () => { throw new Error('poll not started'); };
  const poll = vi.spyOn(api, 'getSwarmResults').mockImplementation(() => new Promise(resolve => { finishPoll = resolve; }));
  const chat = vi.spyOn(api, 'chat').mockImplementation((_message, onEvent) => {
    event = onEvent;
    return () => {};
  });
  let activeSession = 'pending-A';
  vi.spyOn(api, 'readEventsSince').mockImplementation(async () => ({ session_id: activeSession, cursor: 0, events: [] }));
  vi.stubGlobal('fetch', withEndpointDiscovery(async input => {
    const path = String(input).split('?')[0];
    const payload = path === '/api/session/queue' ? { ok: true, session_id: activeSession, items: [], recovery: [] }
      : path === '/api/sessions/transcript' ? { history: [{ role: 'user', content: 'prior turn' }], display }
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
  const send = async () => {
    fireEvent.change(screen.getByPlaceholderText('Message the pilot...'), { target: { value: 'run lifecycle command' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send', exact: true })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Send', exact: true }));
  };
  await send();
  return {
    emit: async (...events: Parameters<typeof event>[0][]) => {
      await act(async () => { events.forEach(e => event(e)); });
      expect(getActiveDiagnostic()).toBeNull();
    },
    drain: async (...results: Awaited<ReturnType<typeof api.getSwarmResults>>['results']) => {
      await waitFor(() => expect(poll).toHaveBeenCalled());
      await act(async () => finishPoll({ results }));
    },
    switchTo: async (id: string) => {
      activeSession = id;
      view.rerender(<Conversation {...props} activeSessionId={id} />);
      await act(async () => { await Promise.resolve(); });
    },
    staleEvent: () => event,
    pendingDrain: async () => {
      await waitFor(() => expect(poll).toHaveBeenCalled());
      return finishPoll;
    },
    send,
    chat,
  };
}

const finalAnswer = { kind: 'message', data: { text: 'The lifecycle command finished successfully.' } };
const naturalDone = { kind: 'assistant_done', data: { stop_cause: 'natural' } };

function expectDone() {
  expect(screen.queryByTitle('Open live activity')).toBeNull();
  expect(screen.queryAllByText('Still working…')).toHaveLength(0);
  expect(screen.getByText('Done', { exact: true })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
  expect(screen.getByText(finalAnswer.data.text)).toBeTruthy();
}

it('settles terminal action_result and assistant_done in one React batch', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(receipt('completed'), finalAnswer, naturalDone);
  expectDone();
});

it('does not resurrect a terminal command from a late running receipt', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(receipt('completed'));
  await stream.emit(finalAnswer, naturalDone);
  expectDone();
  await stream.emit(receipt('running'));
  expectDone();
});

it('preserves natural uninterrupted completion', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(receipt('completed'));
  await stream.emit(finalAnswer, naturalDone);
  expectDone();
});

it('keeps real background work visible after the assistant finishes', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
  expect(screen.getByText(finalAnswer.data.text)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Stop', exact: true })).toBeNull();
});

it('clears awaiting chrome when the last command completes through SSE after the final answer', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
  await stream.emit(receipt('completed'));
  expectDone();
});

it('reconciles a polled terminal command and rejects its delayed running receipt', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  await stream.drain(receipt('completed'));
  expectDone();
  await stream.emit(receipt('running'));
  expectDone();
});

it('does not treat a polled running receipt as terminal', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  await stream.drain(receipt('running'));
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
});

it('does not leak terminal identities or old stream events across session switches', async () => {
  const stream = await mount();
  await stream.emit(receipt('completed'));
  const oldEvent = stream.staleEvent();
  await stream.switchTo('pending-B');
  await stream.send();
  await act(async () => oldEvent(receipt('completed')));
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
  expect(stream.chat).toHaveBeenCalledTimes(2);
});

it('keeps a different running command pending when its sibling completes in the terminal batch', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'), receipt('running', 'local-command-sibling'));
  await stream.emit(receipt('completed'), finalAnswer, naturalDone);
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
  await stream.drain(receipt('completed', 'local-command-sibling'));
  expectDone();
});

it('retains terminal knowledge when a new turn creates a fresh event handler', async () => {
  const stream = await mount();
  await stream.emit(receipt('completed'));
  await stream.emit(finalAnswer, naturalDone);
  await stream.send();
  await stream.emit(receipt('running'));
  await stream.emit({ kind: 'message', data: { text: 'The follow-up is complete.' } }, naturalDone);
  expectDone();
  expect(screen.getByText('The follow-up is complete.')).toBeTruthy();
});

it('ignores a terminal drain from an earlier A epoch after an A-B-A switch', async () => {
  const stream = await mount();
  await stream.emit(receipt('running'));
  const oldDrain = await stream.pendingDrain();
  await stream.switchTo('pending-B');
  await stream.switchTo('pending-A');
  await stream.send();
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  await act(async () => oldDrain({ results: [receipt('completed')] }));
  expect(screen.getByTitle('Open live activity')).toBeTruthy();
});

it('honors an authoritative terminal command receipt loaded from the transcript', async () => {
  const terminal = receipt('completed').data;
  const stream = await mount([
    { type: 'message', role: 'user', text: 'prior turn' },
    { type: 'card', id: terminal.id, kind: 'run_command', goal: 'saved command', result: terminal },
  ]);
  await stream.emit(receipt('running'));
  await stream.emit(finalAnswer, naturalDone);
  expectDone();
});
