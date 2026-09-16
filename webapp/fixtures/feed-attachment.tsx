import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import Conversation from '../src/components/Conversation';
import { api } from '../src/lib/api';
import { withEndpointDiscovery } from '../src/__tests__/endpointFixture';
import '../src/index.css';

let activeSession: string | null = null;
let emit: Parameters<typeof api.chat>[1] | null = null;
let chunk = 0;
const unexpectedRequests: string[] = [];
const transcripts = new Map<string, { type: string; role: string; text: string }[]>();
let answer: { type: string; role: string; text: string } | null = null;
function display() {
  const key = activeSession ?? '';
  let rows = transcripts.get(key);
  if (!rows) {
    rows = [{ type: 'message', role: 'user', text: Array.from({ length: 50 }, (_, i) => `Saved line ${i}`).join('\n\n') }];
    transcripts.set(key, rows);
  }
  return rows;
}
api.chat = (message, onEvent) => {
  answer = { type: 'message', role: 'assistant', text: '' };
  display().push({ type: 'message', role: 'user', text: message }, answer);
  emit = onEvent;
  return () => { emit = null; };
};
api.readEventsSince = async () => ({ session_id: activeSession ?? '', cursor: 0, events: [] });
window.fetch = withEndpointDiscovery(async input => {
  const path = String(input).split('?')[0];
  const payload = path === '/api/session/queue' ? { ok: true, session_id: activeSession, items: [], recovery: [] }
    : path === '/api/sessions/transcript' ? {
      history: [{ role: 'user', content: 'Saved transcript' }],
      display: display(),
    }
    : path === '/api/session/state' ? { state: 'idle', runners: {}, pending_swarms: false }
    : path === '/api/context/usage' ? { available: true, session_id: activeSession, total: 0, limit: 10000, categories: [] }
    : path === '/api/commands' ? { commands: [] }
    : path === '/api/swarm/live' ? []
    : path === '/api/workspace/files' ? { files: [], folders: [] }
    : path === '/api/pilots' ? { pilots: [] }
    : path === '/api/workspace' ? { root: '', files: [], folders: [] }
    : path === '/api/config' ? {}
    : null;
  if (payload === null) unexpectedRequests.push(path);
  return Response.json(payload ?? {});
});
function requiredElement(selector: string) {
  const element = document.querySelector(selector);
  if (!(element instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
  return element;
}
Object.assign(window, { feedFixture: {
  grow() {
    if (!emit) throw new Error('Send a message before growing the stream; unexpected requests: ' + unexpectedRequests.join(', '));
    chunk += 1;
    const text = `\n\nStreaming line ${chunk}: ${'visible live content '.repeat(35)}`;
    if (!answer) throw new Error('Missing durable fixture answer');
    answer.text += text;
    emit({ kind: 'message_delta', data: { stream_id: `fixture-${activeSession}-${transcripts.get(activeSession ?? '')?.length}`, channel: 'answer', text } });
    return chunk;
  },
  measure() {
    const feed = requiredElement('[data-testid="transcript-feed-scrollport"]');
    const composer = requiredElement('[data-testid="composer-chrome"]');
    const paragraphs = Array.from(feed.querySelectorAll('p'));
    const last = paragraphs.at(-1);
    if (!last || !last.textContent?.includes(`Streaming line ${chunk}:`)) throw new Error('Latest stream payload is missing');
    const anchor = Array.from(feed.querySelectorAll<HTMLElement>('[data-viewport-key]')).find(row => row.getBoundingClientRect().bottom > feed.getBoundingClientRect().top);
    return { anchorKey: anchor?.dataset.viewportKey, anchorOffset: anchor ? feed.getBoundingClientRect().top - anchor.getBoundingClientRect().top : null, session: feed.dataset.sessionPane, scrollTop: feed.scrollTop,
      tailDistance: feed.scrollHeight - feed.clientHeight - feed.scrollTop,
      latestBottom: last.getBoundingClientRect().bottom, composerTop: composer.getBoundingClientRect().top,
      unexpectedRequests: [...unexpectedRequests] };
  },
} });
function Fixture() {
  const [session, setSession] = useState<string | null>(null);
  return <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
    <nav>{['fixture-A', 'fixture-B'].map(id => <button key={id} data-switch={id} onClick={() => { activeSession = id; setSession(id); }}>{id}</button>)}</nav>
    <Conversation config={null} activeSessionId={session} onArtifacts={() => {}} onJobChange={() => {}} />
  </div>;
}
const root = document.getElementById('root');
if (!root) throw new Error('Missing fixture root');
createRoot(root).render(<Fixture />);
