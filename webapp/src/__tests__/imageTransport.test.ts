import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { endpointDescriptor } from './endpointFixture';

beforeEach(() => { vi.resetModules(); Reflect.set(window, '__HARNESS_TOKEN__', 'browser-test-token'); });
afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(window, '__HARNESS_TOKEN__'); Reflect.deleteProperty(window, 'harnessIPC'); Reflect.deleteProperty(window, '__HARNESS_PORT__'); });
const imageResponse = () => new Response('pixels', {headers: {'Content-Type': 'image/png'}});

it('authenticates same-origin images with the current credential and all endpoint headers, ignoring a browser port global', async () => {
  Reflect.set(window, '__HARNESS_PORT__', 9999);
  const fetchMock = vi.fn<typeof fetch>(async (path, init) => {
    if (path === '/api/endpoint') return Response.json(endpointDescriptor);
    expect(path).toBe('/api/image?path=input%3As%3Asha');
    expect(init).toMatchObject({cache: 'no-store', credentials: 'omit', redirect: 'error'});
    expect(init?.headers).toMatchObject({'X-Harness-Token': 'browser-test-token', 'X-Harness-Protocol': '1', 'X-Harness-Endpoint': 'fixture-endpoint', 'X-Harness-Boot': 'fixture-boot'});
    return imageResponse();
  });
  vi.stubGlobal('fetch', fetchMock);
  const {fetchImage} = await import('../lib/transport');
  const result = await fetchImage('/api/image?path=input%3As%3Asha', new AbortController().signal);
  expect(result.blob.type).toBe('image/png');
  expect(result.blob.size).toBe(6);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  Reflect.set(window, '__HARNESS_TOKEN__', 'changed');
  expect(result.assertCurrent).toThrow(/connection changed/);
});

it.each(['https://evil.example/api/image?path=x', '//evil.example/api/image?path=x', 'file:///api/image?path=x', '/api/other?path=x', '/api/image?path=x&token=secret', '/api/image?path=x&path=y'])('refuses %s before any credentialed network call', async source => {
  vi.stubGlobal('fetch', vi.fn());
  const {fetchImage} = await import('../lib/transport');
  await expect(fetchImage(source, new AbortController().signal)).rejects.toThrow(/Refusing/);
  expect(fetch).not.toHaveBeenCalled();
});

it.each([
  () => new Response('error', {status:403}),
  () => new Response('html', {headers:{'Content-Type':'text/html'}}),
  () => new Response('', {headers:{'Content-Type':'image/png'}}),
  () => new Response('x', {headers:{'Content-Type':'image/png', 'Content-Length':String(33 * 1024 * 1024)}}),
  () => new Response(new Uint8Array(33 * 1024 * 1024), {headers:{'Content-Type':'image/png'}}),
])('rejects HTTP failures, non-images, empty and oversized bodies', async response => {
  vi.stubGlobal('fetch', vi.fn(async path => path === '/api/endpoint' ? Response.json(endpointDescriptor) : response()));
  const {fetchImage} = await import('../lib/transport');
  await expect(fetchImage('/api/image?path=x', new AbortController().signal)).rejects.toThrow();
});

it('invalidates a stale boot and discovers again on a later bounded retry', async () => {
  let boot = 'a'; let discoveries = 0;
  vi.stubGlobal('fetch', vi.fn(async (path, init) => {
    if (path === '/api/endpoint') { discoveries++; return Response.json({...endpointDescriptor, boot_id:boot}); }
    return new Headers(init?.headers).get('X-Harness-Boot') === boot ? imageResponse() : Response.json({code:'boot_mismatch'}, {status:409});
  }));
  const {fetchImage} = await import('../lib/transport');
  const first = await fetchImage('/api/image?path=x', new AbortController().signal);
  boot = 'b';
  await expect(fetchImage('/api/image?path=x', new AbortController().signal)).rejects.toThrow(/connection changed/);
  expect(first.assertCurrent).toThrow();
  await fetchImage('/api/image?path=x', new AbortController().signal);
  expect(discoveries).toBe(2);
});

it('rejects a delayed body after another request replaces endpoint pins', async () => {
  let boot = 'a'; let body: ReadableStreamDefaultController<Uint8Array> | undefined;
  const stream = new ReadableStream<Uint8Array>({start(controller) { body = controller; }});
  vi.stubGlobal('fetch', vi.fn(async (path, init) => {
    if (path === '/api/endpoint') return Response.json({...endpointDescriptor, boot_id:boot});
    if (path === '/api/image?path=slow') return new Response(stream, {headers:{'Content-Type':'image/png'}});
    return new Headers(init?.headers).get('X-Harness-Boot') === boot ? imageResponse() : Response.json({code:'boot_mismatch'}, {status:409});
  }));
  const {fetchImage} = await import('../lib/transport');
  const slow = fetchImage('/api/image?path=slow', new AbortController().signal);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  boot = 'b';
  await expect(fetchImage('/api/image?path=fast', new AbortController().signal)).rejects.toThrow();
  body?.enqueue(new Uint8Array([1])); body?.close();
  await expect(slow).rejects.toThrow(/connection changed/);
});

function nativeBridge(requestImage: (path: string, identity: Record<string, string>, port: number, done: (value: unknown) => void) => () => void) {
  Reflect.set(window, '__HARNESS_PORT__', 7788);
  Reflect.set(window, 'harnessIPC', {endpointHeaders:true, requestImage, requestJSON:vi.fn(async () => ({kind:'response', status:200, text:JSON.stringify(endpointDescriptor), correlationId:''}))});
  vi.stubGlobal('fetch', vi.fn());
}
const nativeResponse = (bytes: unknown = new Uint8Array([1,2,3])) => ({kind:'image-response', status:200, mime:'image/png', bytes, port:7788});

it.each([new Uint8Array([1,2,3]), new Uint8Array([1,2,3]).buffer])('uses native binary bridge with pin and port, converting bytes to a Blob', async bytes => {
  nativeBridge((path, identity, port, done) => {
    expect(path).toBe('/api/image?path=x');
    expect(port).toBe(7788);
    expect(identity).toEqual({'X-Harness-Protocol':'1', 'X-Harness-Endpoint':'fixture-endpoint', 'X-Harness-Boot':'fixture-boot'});
    done(nativeResponse(bytes)); return () => {};
  });
  const {fetchImage} = await import('../lib/transport');
  const result = await fetchImage('/api/image?path=x', new AbortController().signal);
  expect(result.blob.size).toBe(3);
  expect(result.blob.type).toBe('image/png');
  expect(fetch).not.toHaveBeenCalled();
  Reflect.set(window, '__HARNESS_PORT__', 7799);
  expect(result.assertCurrent).toThrow();
  await expect(fetchImage('http://127.0.0.1:7788/api/image?path=x', new AbortController().signal)).rejects.toThrow(/Refusing/);
});

it('requires native image method and never falls back to file-origin fetch', async () => {
  nativeBridge(() => () => {});
  Reflect.deleteProperty(Reflect.get(window, 'harnessIPC'), 'requestImage');
  const {fetchImage} = await import('../lib/transport');
  await expect(fetchImage('/api/image?path=x', new AbortController().signal)).rejects.toThrow(/bridge needs an update/);
  expect(fetch).not.toHaveBeenCalled();
});

it.each([
  {...nativeResponse(), status:403}, {...nativeResponse(), mime:'text/html'},
  nativeResponse([]), nativeResponse(new Uint8Array(0)), nativeResponse(new Uint8Array(33*1024*1024)),
  {...nativeResponse(), port:7799}, {...nativeResponse(), status:NaN},
])('rejects invalid native responses', async response => {
  nativeBridge((_path, _identity, _port, done) => {done(response); return () => {};});
  const {fetchImage} = await import('../lib/transport');
  await expect(fetchImage('/api/image?path=x', new AbortController().signal)).rejects.toThrow();
});

it('native abort cancels pending IPC and discards a late result', async () => {
  let deliver: ((value: unknown) => void) | undefined;
  const cancel = vi.fn();
  nativeBridge((_path, _identity, _port, done) => {deliver=done; return cancel;});
  const {fetchImage} = await import('../lib/transport');
  const controller = new AbortController();
  const pending = fetchImage('/api/image?path=x', controller.signal);
  await vi.waitFor(() => expect(deliver).toBeDefined());
  controller.abort();
  await expect(pending).rejects.toThrow(/abort/i);
  deliver?.(nativeResponse());
  expect(cancel).toHaveBeenCalledOnce();
});

it('rejects late native errors against stale pins before interpreting the error', async () => {
  let deliver: ((value: unknown) => void) | undefined;
  nativeBridge((_path, _identity, _port, done) => {deliver=done; return () => {};});
  const {fetchImage} = await import('../lib/transport');
  const pending = fetchImage('/api/image?path=x', new AbortController().signal);
  await vi.waitFor(() => expect(deliver).toBeDefined());
  Reflect.set(window, '__HARNESS_PORT__', 7799);
  deliver?.({kind:'image-error',code:'connection'});
  await expect(pending).rejects.toThrow(/connection changed/);
});

it('suppresses an aborted response even when the fetch adapter ignores AbortSignal', async () => {
  const controller = new AbortController();
  vi.stubGlobal('fetch', vi.fn(async path => {
    if (path === '/api/endpoint') return Response.json(endpointDescriptor);
    controller.abort(); return imageResponse();
  }));
  const {fetchImage} = await import('../lib/transport');
  await expect(fetchImage('/api/image?path=x', controller.signal)).rejects.toThrow(/abort/i);
});

it('abort settles a hanging shared discovery for this image consumer', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
  const {fetchImage} = await import('../lib/transport');
  const controller = new AbortController();
  const pending = fetchImage('/api/image?path=x', controller.signal);
  controller.abort();
  await expect(pending).rejects.toThrow(/abort/i);
});

it('abort cancels a stalled body and releases its reader', async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({cancel});
  vi.stubGlobal('fetch', vi.fn(async path => path === '/api/endpoint' ? Response.json(endpointDescriptor) : new Response(body, {headers:{'Content-Type':'image/png'}})));
  const {fetchImage} = await import('../lib/transport');
  const controller = new AbortController();
  const pending = fetchImage('/api/image?path=x', controller.signal);
  await vi.waitFor(() => expect(body.locked).toBe(true));
  controller.abort();
  await expect(pending).rejects.toThrow(/abort/i);
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});

it('a late native connection error cannot invalidate a replacement pin', async () => {
  let slow: ((value: unknown) => void) | undefined;
  let mismatch = true;
  nativeBridge((path, _identity, _port, done) => {
    if (path.includes('slow')) slow = done;
    else if (mismatch) { mismatch = false; done({...nativeResponse(), status:409}); }
    else done(nativeResponse());
    return () => {};
  });
  const {fetchImage} = await import('../lib/transport');
  const pending = fetchImage('/api/image?path=slow', new AbortController().signal);
  await vi.waitFor(() => expect(slow).toBeDefined());
  await expect(fetchImage('/api/image?path=fast', new AbortController().signal)).rejects.toThrow(/connection changed/);
  const replacement = await fetchImage('/api/image?path=fast', new AbortController().signal);
  slow?.({kind:'image-error',code:'connection'});
  await expect(pending).rejects.toThrow(/connection changed/);
  expect(() => replacement.assertCurrent()).not.toThrow();
});

it('one native image socket failure keeps the pin when the backend still answers with the same boot', async () => {
  let slow: ((value: unknown) => void) | undefined;
  nativeBridge((path, _identity, _port, done) => {
    if (path.includes('slow')) slow = done;
    else done({kind:'image-error', code:'connection'});
    return () => {};
  });
  const {fetchImage} = await import('../lib/transport');
  const pending = fetchImage('/api/image?path=slow', new AbortController().signal);
  await vi.waitFor(() => expect(slow).toBeDefined());
  await expect(fetchImage('/api/image?path=broken', new AbortController().signal)).rejects.toThrow(/Native image request failed/);
  slow?.(nativeResponse());
  const result = await pending;
  expect(() => result.assertCurrent()).not.toThrow();
});

it('one fetched image socket failure keeps the pin when the backend still answers with the same boot', async () => {
  let release: (() => void) | undefined;
  const gate = new Promise<void>(resolve => { release = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/endpoint') return Response.json(endpointDescriptor);
    if (path.includes('slow')) { await gate; return imageResponse(); }
    throw new TypeError('Failed to fetch');
  }));
  const {fetchImage} = await import('../lib/transport');
  const pending = fetchImage('/api/image?path=slow', new AbortController().signal);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await expect(fetchImage('/api/image?path=broken', new AbortController().signal)).rejects.toThrow(/Failed to fetch/);
  release?.();
  const result = await pending;
  expect(() => result.assertCurrent()).not.toThrow();
});
