import { parseJSONResponse, type JSONResponse } from '../../electron/json-response.mjs';

type Endpoint = { kind: 'versioned'; endpoint: string; boot: string; replay: boolean } | { kind: 'legacy' };
type Discovery = () => Promise<JSONResponse>;
export type EndpointRequest = { pin: Endpoint; path: string; session: string | null; reset: boolean; ringReset: boolean; sequence: number };

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function endpointRecoveryError(): Error {
  return Object.assign(new Error('Backend connection changed; retry this action explicitly. Reload this page or restart the local app if recovery keeps failing.'), {code:'ENDPOINT_RECONNECT_REQUIRED'});
}
const ENDPOINT_ROTATION_CODES = new Set(['endpoint_mismatch', 'boot_mismatch']);

/**
 * A stream error that means the backend endpoint/boot rotated. Other 409s
 * (ring-watch miss, input_session_changed) are session-scoped and must not
 * drop the global endpoint pin.
 */
export function isEndpointRotationError(error: unknown): boolean {
  return record(error) && error.status === 409
    && typeof error.code === 'string' && ENDPOINT_ROTATION_CODES.has(error.code);
}
export function isEndpointMismatch(response: JSONResponse): boolean {
  if (response.kind !== 'response' || response.status !== 409) return false;
  try {
    const body: unknown = JSON.parse(response.text);
    return record(body) && typeof body.code === 'string' && ENDPOINT_ROTATION_CODES.has(body.code);
  } catch { return false; }
}

export class EndpointSessionClient {
  private pin: Endpoint | undefined;
  private pending: Promise<Endpoint> | undefined;
  private streams = new Map<string, { id: string; sequence: number }>();
  private sequence = 0;
  private ringSessions = new Set<string>();

  connect(discover: Discovery): Promise<Endpoint> {
    if (this.pin) return Promise.resolve(this.pin);
    if (this.pending) return this.pending;
    this.pending = (async () => {
      const response = await discover();
      if (response.kind === 'response' && response.status === 404) {
        const pin: Endpoint = {kind:'legacy'};
        this.pin = pin;
        return pin;
      }
      const value: unknown = parseJSONResponse(response, '/api/endpoint');
      if (!record(value) || value.ok !== true || value.protocol_version !== 1
        || typeof value.endpoint_id !== 'string' || !value.endpoint_id
        || typeof value.boot_id !== 'string' || !value.boot_id
        || !Array.isArray(value.capabilities) || !value.capabilities.includes('endpoint_fence_v1')) {
        throw new Error('Unsupported endpoint handshake. Update or restart the local backend, then reload this page.');
      }
      const pin: Endpoint = {kind:'versioned', endpoint:value.endpoint_id, boot:value.boot_id,
        replay:value.capabilities.includes('session_replay_fence_v1')};
      this.pin = pin;
      return pin;
    })().finally(() => { this.pending = undefined; });
    return this.pending;
  }

  isCurrent(pin: Endpoint): boolean { return this.pin === pin; }
  invalidate(pin: Endpoint): void {
    if (!this.isCurrent(pin)) return;
    this.pin = undefined;
    this.streams.clear();
    this.ringSessions.clear();
  }
  headers(pin: Endpoint): Record<string,string> {
    return pin.kind === 'legacy' ? {} : {'X-Harness-Protocol':'1', 'X-Harness-Endpoint':pin.endpoint, 'X-Harness-Boot':pin.boot};
  }
  prepare(path: string, pin: Endpoint): EndpointRequest {
    if (!this.isCurrent(pin)) throw endpointRecoveryError();
    const url = new URL(path, 'http://harness.local');
    let ringReset = false;
    const ring = pin.kind === 'versioned' && url.pathname === '/api/chat/events';
    if (ring) {
      const sid = url.searchParams.get('session');
      if (!sid?.trim()) throw new Error('Chat replay requires an explicit session. Select a conversation and retry.');
      if (!this.ringSessions.has(sid)) {
        ringReset = true;
        url.searchParams.set('since','0');
        url.searchParams.delete('generation');
        this.ringSessions.add(sid);
      }
    }
    const replay = pin.kind === 'versioned' && pin.replay && url.pathname === '/api/session/events';
    const session = replay ? url.searchParams.get('session') || url.searchParams.get('session_id') : null;
    let reset = false;
    if (replay) {
      if (!session?.trim()) throw new Error('Session replay requires an explicit session. Select a conversation and retry.');
      const saved = this.streams.get(session);
      if (saved) url.searchParams.set('stream_id', saved.id);
      else {
        reset = true;
        url.searchParams.set('since','0');
        url.searchParams.delete('generation');
        url.searchParams.delete('stream_id');
      }
    }
    return {pin, path: replay || ring ? url.pathname + url.search : path, session, reset, ringReset, sequence:++this.sequence};
  }
  accept(request: EndpointRequest, value: unknown): unknown {
    if (!this.isCurrent(request.pin)) throw endpointRecoveryError();
    if (!request.session) return value;
    if (!record(value) || value.session_id !== request.session || typeof value.stream_id !== 'string' || !value.stream_id) {
      throw new Error('Invalid session replay identity. Reopen the conversation.');
    }
    const saved = this.streams.get(request.session);
    if (saved && saved.sequence > request.sequence) throw endpointRecoveryError();
    this.streams.set(request.session,{id:value.stream_id,sequence:request.sequence});
    return {...value, replay_reset:request.reset};
  }
  resetReplay(request: EndpointRequest): void {
    if (!this.isCurrent(request.pin) || !request.session) return;
    const saved = this.streams.get(request.session);
    if (!saved || saved.sequence <= request.sequence) this.streams.delete(request.session);
  }
}
