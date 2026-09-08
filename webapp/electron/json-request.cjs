const http = require('node:http');
const {performance} = require('node:perf_hooks');

// General JSON includes unpaginated transcripts, not just 5 MiB bundle Offers.
// Keep headroom above the backend's configurable 8 MiB request default.
const MAX_JSON_BYTES = 64 * 1024 * 1024;
const JSON_TIMEOUT_MS = 120000;
const SLAB_BYTES = 64 * 1024;
const messages = {
  JSON_REQUEST_CANCELLED: 'JSON read was cancelled.',
  JSON_REQUEST_TOO_LARGE: 'JSON request exceeds the desktop byte limit. Reduce the payload or use a file/CLI transfer.',
  JSON_RESPONSE_TOO_LARGE: 'JSON response exceeds the desktop byte limit. Use a smaller query or file/CLI export.',
  JSON_REQUEST_INVALID: 'Unable to serialize or send the JSON request. Check the request input.',
  JSON_REQUEST_TIMEOUT: 'Backend JSON request exceeded its deadline. Check backend health.',
  JSON_RESPONSE_LENGTH: 'Backend JSON response length is inconsistent. Check backend health.',
};
function failure(code, method) {
  let message = messages[code] || 'Backend JSON connection failed. Check backend health.';
  if (method !== 'GET' && method !== 'HEAD' && !['JSON_REQUEST_TOO_LARGE', 'JSON_REQUEST_INVALID'].includes(code)) {
    message += ' The write may have completed; check its status before trying again.';
  }
  return Object.assign(new Error(message), {code});
}
function connectionCode(error) {
  return ['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'ETIMEDOUT'].includes(error?.code)
    ? error.code : 'JSON_CONNECTION_ERROR';
}

/** One request, no replay. Policy is main-owned and never accepted over IPC. */
function requestJSONOnce(options, body, {
  request = http.request, maxBytes = MAX_JSON_BYTES, timeoutMs = JSON_TIMEOUT_MS,
  maxRequestBytes = maxBytes, maxResponseBytes = maxBytes, signal,
} = {}) {
  const deadline = performance.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    let req, res, timer, settled = false, size = 0, used = 0;
    let slabs = [], slab;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      slabs = [];
      slab = undefined;
      if (error) {
        reject(error);
        res?.destroy();
        req?.destroy();
      } else resolve(value);
    };
    const fail = code => finish(failure(code, options.method));
    const abort = () => fail('JSON_REQUEST_CANCELLED');
    const expired = () => {
      if (performance.now() < deadline) return false;
      fail('JSON_REQUEST_TIMEOUT');
      return true;
    };
    try {
      if (signal?.aborted) { abort(); return; }
      signal?.addEventListener('abort', abort, {once: true});
      const data = body === undefined ? undefined : JSON.stringify(body);
      const bytes = data === undefined ? 0 : Buffer.byteLength(data);
      if (bytes > maxRequestBytes) { fail('JSON_REQUEST_TOO_LARGE'); return; }
      if (expired()) return;
      timer = setTimeout(() => fail('JSON_REQUEST_TIMEOUT'), Math.max(1, deadline - performance.now()));
      req = request({...options, headers:{...options.headers,
        ...(data !== undefined ? {'Content-Length':bytes} : {}),
      }}, response => {
        res = response;
        res.on('error', error => fail(connectionCode(error)));
        res.on('aborted', () => fail('ECONNRESET'));
        if (settled) { res.destroy(); return; }
        if (expired()) return;
        // HEAD/204/304 may describe a representation without sending its body.
        const hasBody = options.method !== 'HEAD' && ![204, 304].includes(res.statusCode);
        const declared = hasBody ? res.headers['content-length'] : undefined;
        if (declared !== undefined && (!/^\d+$/.test(String(declared)) || !Number.isSafeInteger(Number(declared)))) {
          fail('JSON_RESPONSE_LENGTH'); return;
        }
        if (declared !== undefined && Number(declared) > maxResponseBytes) {
          fail('JSON_RESPONSE_TOO_LARGE'); return;
        }
        res.on('data', chunk => {
          if (settled || expired()) return;
          size += chunk.length;
          if (size > maxResponseBytes) { fail('JSON_RESPONSE_TOO_LARGE'); return; }
          // Slabs also bound bookkeeping when the backend sends tiny chunks.
          let offset = 0;
          while (offset < chunk.length) {
            if (!slab || used === slab.length) {
              slab = Buffer.allocUnsafe(Math.min(SLAB_BYTES, maxResponseBytes));
              slabs.push(slab);
              used = 0;
            }
            const take = Math.min(slab.length - used, chunk.length - offset);
            chunk.copy(slab, used, offset, offset + take);
            used += take;
            offset += take;
          }
        });
        res.on('end', () => {
          if (settled || expired()) return;
          if (declared !== undefined && Number(declared) !== size) { fail('JSON_RESPONSE_LENGTH'); return; }
          if (slabs.length) slabs[slabs.length - 1] = slab.subarray(0, used);
          const text = Buffer.concat(slabs, size).toString('utf8');
          if (expired()) return;
          finish(null, {kind:'response', status:res.statusCode, text,
            correlationId:String(res.headers['x-correlation-id'] || ''),
          });
        });
      });
      req.on('error', error => fail(connectionCode(error)));
      if (data !== undefined) req.write(data);
      req.end();
    } catch {
      fail(req ? 'JSON_CONNECTION_ERROR' : 'JSON_REQUEST_INVALID');
    }
  });
}
module.exports = {requestJSONOnce, MAX_JSON_BYTES, JSON_TIMEOUT_MS};
