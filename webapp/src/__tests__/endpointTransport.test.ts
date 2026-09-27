import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const descriptor = (boot = 'a') => ({ok:true,protocol_version:1,endpoint_id:'endpoint',boot_id:boot,capabilities:['endpoint_fence_v1','session_replay_fence_v1']});
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body),{status});
beforeEach(() => vi.resetModules());
afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(window,'harnessIPC'); });
it('pins browser writes, refuses stale writes and rediscovers without replay', async () => {
  let boot = 'a'; let writes = 0; let discoveries = 0;
  vi.stubGlobal('fetch',vi.fn(async (path, options) => {
    if (path === '/api/endpoint') { discoveries++; return response(descriptor(boot)); }
    expect(options.headers['X-Harness-Endpoint']).toBe('endpoint');
    if (options.headers['X-Harness-Boot'] !== boot) return response({code:'boot_mismatch',error:'stale'},409);
    writes++; return response({ok:true});
  }));
  const {postJSON} = await import('../lib/transport');
  await postJSON('/write',{}); boot = 'b';
  await expect(postJSON('/write',{})).rejects.toThrow(/retry this action explicitly/);
  expect(writes).toBe(1); expect(discoveries).toBe(2);
  await postJSON('/write',{}); expect(writes).toBe(2);
});
it('a delayed response from the prior boot cannot repaint or reset new pins', async () => {
  let boot = 'a'; let resolveOld: (value: Response)=>void = () => {};
  vi.stubGlobal('fetch',vi.fn(async (path, options) => {
    if (path === '/api/endpoint') return response(descriptor(boot));
    if (path === '/slow') return new Promise<Response>(resolve => {resolveOld=resolve;});
    if (options.headers['X-Harness-Boot'] !== boot) return response({code:'boot_mismatch'},409);
    return response({ok:true});
  }));
  const {getJSON} = await import('../lib/transport');
  const slow = getJSON('/slow');
  await vi.waitFor(()=>expect(vi.mocked(fetch).mock.calls.length).toBe(2));
  boot='b'; await expect(getJSON('/fast')).rejects.toThrow();
  resolveOld(response({old:true})); await expect(slow).rejects.toThrow();
  await expect(getJSON('/fast')).resolves.toEqual({ok:true});
});
it('rejects auth/unsupported discovery and only falls back on 404', async () => {
  let status=403;
  vi.stubGlobal('fetch',vi.fn(async path=>path==='/api/endpoint' ? response({},status):response({ok:true})));
  const {postJSON} = await import('../lib/transport');
  await expect(postJSON('/write',{})).rejects.toThrow(); expect(fetch).toHaveBeenCalledTimes(1);
  status=404; await expect(postJSON('/write',{})).resolves.toEqual({ok:true});
  expect(vi.mocked(fetch).mock.calls[2][1]?.headers).not.toHaveProperty('X-Harness-Protocol');
});
it('preserves explicit replay session and resets consumer cursors after new handshake', async () => {
  const paths: string[] = [];
  vi.stubGlobal('fetch',vi.fn(async path=> {
    if (path==='/api/endpoint') return response(descriptor());
    paths.push(path); return response({session_id:'s',stream_id:'opaque',cursor:3,events:[]});
  }));
  const {getJSON,sessionEventsPath} = await import('../lib/transport');
  expect(await getJSON(sessionEventsPath({session:'s',since:99,generation:4}))).toMatchObject({replay_reset:true});
  expect(paths[0]).toContain('since=0'); expect(paths[0]).not.toContain('generation');
  await getJSON(sessionEventsPath({session:'s',since:3})); expect(paths[1]).toContain('stream_id=opaque');
});
it('pins browser uploads and SSE and suppresses callbacks after cancellation', async () => {
  const seen: {path:string;headers:Record<string,string>}[]=[];
  vi.stubGlobal('fetch',vi.fn(async (path, options)=> {
    if (path==='/api/endpoint') return response(descriptor());
    seen.push({path,headers:options.headers});
    if(path==='/api/upload') return response({saved:[]});
    return new Response('data: {"kind":"message"}\n\ndata: {"kind":"done"}\n\n');
  }));
  const {uploadFile,stream} = await import('../lib/transport');
  await uploadFile(new File(['hello'],'file.txt'));
  const events=vi.fn(); const done=vi.fn();
  stream('/api/chat?session=s',events,done);
  await vi.waitFor(()=>expect(done).toHaveBeenCalledOnce());
  expect(events).toHaveBeenCalledOnce();
  expect(seen.every(r=>r.headers['X-Harness-Boot']==='a')).toBe(true);
  const cancelled=vi.fn(); stream('/api/chat?session=s',cancelled)();
  await Promise.resolve(); expect(cancelled).not.toHaveBeenCalled();
});
it('fails only the stream on a session-scoped 409 and rediscovers on endpoint rotation', async () => {
  let discoveries = 0; let body: unknown = {ok:false, error:'ring miss'};
  vi.stubGlobal('fetch',vi.fn(async path => {
    if (path==='/api/endpoint') { discoveries++; return response(descriptor()); }
    return response(body, 409);
  }));
  const {stream} = await import('../lib/transport');
  for (const scoped of [{ok:false,error:'ring miss'}, {ok:false,code:'input_session_changed'}]) {
    body = scoped;
    const onError = vi.fn();
    stream('/api/chat/events?watch=1&session=s', vi.fn(), vi.fn(), onError);
    await vi.waitFor(()=>expect(onError).toHaveBeenCalledOnce());
    expect(onError.mock.calls[0][0]).toMatchObject({status:409});
  }
  await Promise.resolve();
  expect(discoveries).toBe(1);
  body = {ok:false, code:'endpoint_mismatch'};
  const onError = vi.fn();
  stream('/api/chat/events?watch=1&session=s', vi.fn(), vi.fn(), onError);
  await vi.waitFor(()=>expect(onError).toHaveBeenCalledOnce());
  await vi.waitFor(()=>expect(discoveries).toBe(2));
});
it('shows a scoped reconnect action without replaying the failed mutation', async () => {
  let boot='a';
  vi.stubGlobal('fetch',vi.fn(async (path,options)=> path==='/api/endpoint' ? response(descriptor(boot))
    : options.headers['X-Harness-Boot']===boot ? response({ok:true}) : response({code:'boot_mismatch'},409)));
  const {postJSON}=await import('../lib/transport');
  const {getActiveDiagnostic,resetDiagnosticBus}=await import('../lib/operationalDiagnosticBus');
  resetDiagnosticBus();
  await postJSON('/write',{session:'s'}); boot='b';
  await expect(postJSON('/write',{session:'s'},{failureKind:'action'})).rejects.toThrow();
  expect(getActiveDiagnostic()).toMatchObject({sessionId:'s',code:'ENDPOINT_RECONNECT_REQUIRED',recovery:{kind:'retry',label:'Reconnect'}});
});
it('keeps the pin after a socket reset when the backend identity is unchanged', async () => {
  const {EndpointSessionClient} = await import('../lib/endpointSession');
  let boot = 'a'; let reachable = true;
  const discover = async () => {
    if (!reachable) throw Object.assign(new Error('connect ECONNREFUSED'), {code:'ECONNREFUSED'});
    return {kind:'response' as const, status:200, text:JSON.stringify(descriptor(boot))};
  };
  const client = new EndpointSessionClient();
  const pin = await client.connect(discover);
  await client.revalidate(pin, discover);
  expect(client.isCurrent(pin)).toBe(true);
  boot = 'b';
  await client.revalidate(pin, discover);
  expect(client.isCurrent(pin)).toBe(false);
  const next = await client.connect(discover);
  reachable = false;
  await client.revalidate(next, discover);
  expect(client.isCurrent(next)).toBe(false);
});
it('a request reset mid-switch does not raise reconnect on concurrent requests', async () => {
  let resetOnce = true;
  vi.stubGlobal('fetch',vi.fn(async path => {
    if (path === '/api/endpoint') return response(descriptor());
    if (path === '/api/session/state' && resetOnce) {
      resetOnce = false;
      throw Object.assign(new TypeError('socket hang up'), {code:'ECONNRESET'});
    }
    return response({ok:true});
  }));
  const {getJSON} = await import('../lib/transport');
  const failed = getJSON('/api/session/state');
  const concurrent = getJSON('/api/config');
  await expect(failed).rejects.toThrow();
  await expect(failed).rejects.not.toThrow(/retry this action explicitly/);
  await expect(concurrent).resolves.toEqual({ok:true});
  await expect(getJSON('/api/config')).resolves.toEqual({ok:true});
});
