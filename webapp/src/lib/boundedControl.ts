import type { JSONResponse } from '../../electron/json-response.mjs';

export class ControlRequestError extends Error {
  readonly code: 'outcome_unknown' | 'bounds_exceeded';
  constructor(code: 'outcome_unknown' | 'bounds_exceeded') { super(code); this.code = code; }
}

export async function controlDeadline<T>(request: Promise<T>, cancel?: () => void, timeoutMs = 30000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      cancel?.();
      reject(new ControlRequestError('outcome_unknown'));
    }, timeoutMs);
  });
  try { return await Promise.race([request, deadline]); }
  finally { if (timer !== undefined) clearTimeout(timer); }
}

export async function browserResponse(path: string, init: RequestInit, byteLimit: number, timeoutMs = 30000): Promise<JSONResponse> {
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    return await controlDeadline((async () => {
      const response = await fetch(path, { ...init, signal: controller.signal });
      const declared = response.headers.get('content-length');
      if (declared !== null && Number(declared) > byteLimit) {
        throw new ControlRequestError('bounds_exceeded');
      }
      reader = response.body?.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: true });
      let bytes = 0;
      let text = '';
      while (reader) {
        const next = await reader.read();
        if (next.done) break;
        bytes += next.value.byteLength;
        if (bytes > byteLimit) throw new ControlRequestError('bounds_exceeded');
        text += decoder.decode(next.value, { stream: true });
      }
      text += decoder.decode();
      return { kind: 'response', status: response.status, text, correlationId: '' };
    })(), () => controller.abort(), timeoutMs);
  } finally {
    void reader?.cancel().catch(() => {});
    controller.abort();
  }
}
