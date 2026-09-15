import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ConversationHeader from '../components/conversation/ConversationHeader';
import { getJSONSoft, postJSON } from '../lib/transport';
import { getActiveDiagnostic, resetDiagnosticBus } from '../lib/operationalDiagnosticBus';
import { setCorrelationId } from '../lib/correlationId';
import { useOperationalDiagnostic } from '../lib/useOperationalDiagnostic';
import { api } from '../lib/api';

const handshake = JSON.stringify({ok:true,protocol_version:1,endpoint_id:"test-endpoint",boot_id:"test-boot",capabilities:["endpoint_fence_v1"]});

function Header({ sessionId, repo }: { sessionId?: string; repo?: string }) {
  const diag = useOperationalDiagnostic({ sessionId, repo });
  return <ConversationHeader pillStatus={diag?.severity === 'error' ? 'error' : 'idle'}
    detail={diag?.summary} correlationId={diag?.correlationId}
    recoveryAction={diag && diag.recovery.kind !== 'none' ? { label: diag.recovery.label, onClick() {} } : undefined} />;
}
afterEach(() => { cleanup(); resetDiagnosticBus(); vi.unstubAllGlobals(); Reflect.deleteProperty(window, 'harnessIPC'); });

for (const mode of ['web', 'desktop']) {
  describe(mode, () => {
    function respond(status: number, text: string) {
      if (mode === 'web') vi.stubGlobal('fetch', vi.fn(async path => path === '/api/endpoint' ? new Response(handshake,{status:200}) : new Response(text, {status, headers: {'X-Correlation-Id': 'request-A'}})));
      else Object.defineProperty(window, 'harnessIPC', {configurable: true, value: {endpointHeaders:true,requestJSON: async (_method: string, path: string) => path === '/api/endpoint' ? {kind:'response',status:200,text:handshake,correlationId:''} : ({kind: 'response', status, text, correlationId: 'request-A'})}});
    }
    it.each([400, 409, 422])('keeps a known action %i beside its control without poisoning the header', async status => {
      respond(status, JSON.stringify({error: 'Worktree has uncommitted changes'}));
      const view = render(<Header sessionId="B" repo="/B" />);
      await act(async () => {
        await expect(postJSON('/api/worktrees/remove', {repo: '/A'})).rejects.toMatchObject({status, message: 'Worktree has uncommitted changes'});
      });
      expect(getActiveDiagnostic()).toBeNull();
      expect(screen.queryByText('Error')).toBeNull();
      expect(screen.queryByText('Retry')).toBeNull();
      view.rerender(<Header sessionId="A" repo="/A" />);
      expect(screen.queryByText('Error')).toBeNull();
    });
    it.each([401, 403, 404, 408, 429, 500, 503])('retains operational status %i', async status => {
      respond(status, '{"error":"request refused"}');
      render(<Header sessionId="A" repo="/A" />);
      await act(async () => { await expect(postJSON('/api/worktrees/max', {repo: '/A'})).rejects.toMatchObject({status}); });
      expect(getActiveDiagnostic()?.severity).toBe('error');
      expect(screen.getByText('Error')).toBeTruthy();
      expect(screen.getByText('Retry')).toBeTruthy();
    });
    it.each(['queue_write_failed', 'queue_read_failed'])('keeps %s beside queue controls', async code => {
      respond(503, JSON.stringify({ok: false, code, error: 'Draft retained; queue could not be saved.'}));
      render(<Header sessionId="A" repo="/A" />);
      const requests = [
        () => api.queueList('queue-ui'),
        () => api.queueAdd('draft', [], 'A'),
        () => api.queueRemove('queued-id', 'A'),
        () => api.queueReorder(['queued-id'], 'A'),
        () => api.queueClear('A'),
        () => api.steerSession('draft', [], 'follow_up', {sessionId: 'A'}),
      ];
      for (const request of requests) {
        await act(async () => { await expect(request()).rejects.toMatchObject({status: 503}); });
        expect(getActiveDiagnostic()).toBeNull();
        expect(screen.queryByText('Retry')).toBeNull();
      }
    });
    it('keeps an unrecognized queue 503 operational', async () => {
      respond(503, '{"ok":false,"code":"backend_unavailable","error":"Backend unavailable"}');
      await expect(api.queueAdd('draft', [], 'A')).rejects.toMatchObject({status: 503});
      expect(getActiveDiagnostic()?.severity).toBe('error');
    });
    it('keeps cold queue readiness local while preserving real server failures', async () => {
      respond(409, JSON.stringify({ok:false, code:'pilot_not_ready', error:'Session queue is not ready.'}));
      await expect(api.queueList('queue-ui')).rejects.toMatchObject({status:409, code:'pilot_not_ready'});
      expect(getActiveDiagnostic()).toBeNull();
      respond(500, JSON.stringify({error:'Unexpected server failure'}));
      await expect(api.queueList('queue-ui')).rejects.toMatchObject({status:500});
      expect(getActiveDiagnostic()?.severity).toBe('error');
    });
    it('preserves partial deletion detail without claiming the whole session failed', async () => {
      respond(409, JSON.stringify({code:'lease_exhausted', error:'All slots busy', deleted:'A', active:'B'}));
      await expect(api.deleteSession('A')).rejects.toMatchObject({status:409, deleted:'A', active:'B'});
      expect(getActiveDiagnostic()).toBeNull();
    });
    it('keeps unknown endpoints and malformed action responses operational', async () => {
      for (const [path, body] of [['/api/unknown', '{"error":"bad"}'], ['/api/worktrees/max', '<html>bad</html>']]) {
        respond(400, body);
        await expect(postJSON(path, {})).rejects.toMatchObject({status:400});
        expect(getActiveDiagnostic()?.severity).toBe('error');
        resetDiagnosticBus();
      }
    });
    it('supports explicit action policy and operational override without swallowing errors', async () => {
      respond(409, '{"error":"conflict"}');
      await expect(postJSON('/api/other-action', {}, {failureKind:'action'})).rejects.toMatchObject({status:409});
      expect(getActiveDiagnostic()).toBeNull();
      await expect(postJSON('/api/worktrees/max', {}, {failureKind:'operational'})).rejects.toMatchObject({status:409});
      expect(getActiveDiagnostic()?.severity).toBe('error');
      resetDiagnosticBus();
      expect(await getJSONSoft('/api/other-action', {failureKind:'action'})).toEqual({ok:false, error:'conflict'});
      expect(getActiveDiagnostic()).toBeNull();
    });
    it('does not erase an existing operational failure when an action is rejected', async () => {
      respond(503, '{"error":"not ready"}');
      await expect(postJSON('/api/worktrees/max', {})).rejects.toMatchObject({status:503});
      const prior = getActiveDiagnostic();
      respond(400, '{"error":"dirty worktree"}');
      await expect(postJSON('/api/worktrees/remove', {})).rejects.toMatchObject({status:400});
      expect(getActiveDiagnostic()).toBe(prior);
    });
    it('keeps a late 409 out of both repositories headers', async () => {
      let release: () => void = () => {};
      const held = new Promise<void>(resolve => { release = resolve; });
      const text = '{"error":"workspace changed"}';
      if (mode === 'web') vi.stubGlobal('fetch', async path => { if (path === '/api/endpoint') return new Response(handshake,{status:200}); await held; return new Response(text, {status:409}); });
      else Object.defineProperty(window, 'harnessIPC', {configurable:true, value:{endpointHeaders:true,requestJSON:async (_method: string, path: string) => { if (path === "/api/endpoint") return {kind:"response",status:200,text:handshake,correlationId:""}; await held; return {kind:'response', status:409, text, correlationId:'old-A'}; }}});
      const view = render(<Header sessionId="A" repo="/A" />);
      const pending = postJSON('/api/worktrees/max', {repo:'/A'});
      view.rerender(<Header sessionId="B" repo="/B" />);
      await act(async () => { release(); await expect(pending).rejects.toMatchObject({status:409, message:'workspace changed'}); });
      expect(screen.queryByText('Error')).toBeNull();
      expect(screen.queryByText('Retry')).toBeNull();
      view.rerender(<Header sessionId="A" repo="/A" />);
      expect(screen.queryByText('Error')).toBeNull();
      expect(getActiveDiagnostic()).toBeNull();
    });
    it('filters a late scoped failure on the actual bus and header', async () => {
      let release: () => void = () => {};
      const held = new Promise<void>(resolve => { release = resolve; });
      const envelope = {kind: 'response', status:500, text:'{"error":"server failed"}', correlationId:'request-A'};
      if (mode === 'web') vi.stubGlobal('fetch', async path => { if (path === '/api/endpoint') return new Response(handshake,{status:200}); await held; return new Response(envelope.text, {status:500, headers:{'X-Correlation-Id':'request-A'}}); });
      else Object.defineProperty(window, 'harnessIPC', {configurable:true, value:{endpointHeaders:true,requestJSON:async (_method: string, path: string) => { if (path === "/api/endpoint") return {kind:"response",status:200,text:handshake,correlationId:""}; await held; return envelope; }}});
      const view = render(<Header sessionId="A" repo="/A" />);
      const pending = postJSON('/api/worktrees/max?session=A', {repo:'/A'});
      view.rerender(<Header sessionId="B" repo="/B" />);
      await act(async () => { release(); await expect(pending).rejects.toMatchObject({status:500}); });
      expect(getActiveDiagnostic()).toMatchObject({sessionId:'A', repo:'/A', correlationId:'request-A'});
      expect(screen.queryByText('Error')).toBeNull();
      expect(screen.queryByText('Retry')).toBeNull();
      view.rerender(<Header sessionId="A" repo="/A" />);
      expect(screen.getByText('Error')).toBeTruthy();
      view.rerender(<Header />);
      expect(screen.queryByText('Error')).toBeNull();
    });
  });
}

it.each(['network', 'ECONNRESET'])('keeps %s failure operational without borrowing an old correlation', async code => {
  setCorrelationId('unrelated-old-request');
  vi.stubGlobal('fetch', async () => { throw Object.assign(new Error('connection lost'), {code}); });
  await expect(postJSON('/api/worktrees/max', {}, {sessionId:'A', repo:'/A'})).rejects.toThrow('connection lost');
  expect(getActiveDiagnostic()).toMatchObject({scope:'transport', sessionId:'A', repo:'/A', correlationId:''});
});
