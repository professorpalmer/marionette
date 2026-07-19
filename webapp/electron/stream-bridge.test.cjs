"use strict";

// Unit tests for the harness:stream SSE terminal contract (stream-bridge.cjs).
// Regression for the v0.9.95 update-skew incident: a 403 stream response used
// to fall through the SSE parser into `end` -> `:done`, which the renderer read
// as a clean close and painted the generic "[aborted]" message. Run with
// `node --test electron/*.test.cjs`.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  sanitizedStreamHttpError,
  sanitizedStreamConnError,
  wireStreamResponse,
} = require("./stream-bridge.cjs");

const SECRET_TOKEN = "deadbeefcafe1234deadbeefcafe1234";

function fakeResponse(statusCode) {
  const res = new EventEmitter();
  res.statusCode = statusCode;
  res.setEncoding = () => {};
  res.destroy = () => { res.destroyed = true; };
  return res;
}

function wireWithRecorder(res) {
  const calls = { events: [], done: 0, errors: [] };
  wireStreamResponse(res, {
    onEvent: (ev) => calls.events.push(ev),
    onDone: () => { calls.done += 1; },
    onError: (payload) => calls.errors.push(payload),
  });
  return calls;
}

test("403 response maps to :error (never :done), even when end fires", () => {
  const res = fakeResponse(403);
  const calls = wireWithRecorder(res);
  // Old backend behavior: JSON error body, then stream end.
  res.emit("data", `{"error":"missing or bad token ${SECRET_TOKEN}"}`);
  res.emit("end");
  assert.equal(calls.done, 0, "a 403 must never emit :done");
  assert.equal(calls.errors.length, 1);
  assert.equal(calls.errors[0].status, 403);
  assert.equal(calls.errors[0].code, "auth");
});

test("non-2xx error payload is sanitized: no body text, no token", () => {
  const res = fakeResponse(403);
  const calls = wireWithRecorder(res);
  res.emit("data", `{"error":"secret-body-detail ${SECRET_TOKEN}"}`);
  res.emit("end");
  const serialized = JSON.stringify(calls.errors[0]);
  assert.ok(!serialized.includes(SECRET_TOKEN), "error payload must not carry the token");
  assert.ok(!serialized.includes("secret-body-detail"), "error payload must not carry the response body");
});

test("500 response maps to a backend_error :error", () => {
  const res = fakeResponse(500);
  const calls = wireWithRecorder(res);
  res.emit("end");
  assert.equal(calls.done, 0);
  assert.deepEqual(
    { status: calls.errors[0].status, code: calls.errors[0].code },
    { status: 500, code: "backend_error" }
  );
});

test("2xx SSE frames flow through; done frame ends the stream once", () => {
  const res = fakeResponse(200);
  const calls = wireWithRecorder(res);
  res.emit("data", 'data: {"kind":"delta","data":{"text":"hi"}}\n\n');
  res.emit("data", 'data: {"kind": "done"}\n\n');
  res.emit("end"); // must not double-fire the terminal callback
  assert.equal(calls.events.length, 1);
  assert.equal(calls.events[0].kind, "delta");
  assert.equal(calls.done, 1);
  assert.equal(calls.errors.length, 0);
});

test("2xx stream end without a done frame still emits :done", () => {
  const res = fakeResponse(200);
  const calls = wireWithRecorder(res);
  res.emit("data", 'data: {"kind":"delta","data":{}}\n\n');
  res.emit("end");
  assert.equal(calls.done, 1);
  assert.equal(calls.errors.length, 0);
});

test("2xx response error maps to a sanitized connection :error", () => {
  const res = fakeResponse(200);
  const calls = wireWithRecorder(res);
  const err = new Error(`read ECONNRESET while sending ${SECRET_TOKEN}`);
  err.code = "ECONNRESET";
  res.emit("error", err);
  assert.equal(calls.done, 0);
  assert.equal(calls.errors.length, 1);
  assert.equal(calls.errors[0].code, "ECONNRESET");
  assert.ok(!JSON.stringify(calls.errors[0]).includes(SECRET_TOKEN));
});

test("sanitizedStreamHttpError: auth statuses get the out-of-sync hint", () => {
  for (const status of [401, 403]) {
    const payload = sanitizedStreamHttpError(status);
    assert.equal(payload.code, "auth");
    assert.match(payload.message, /authentication failed/);
    assert.match(payload.message, /out of sync/);
  }
});

test("sanitizedStreamConnError: carries only the errno code", () => {
  const err = new Error(`connect ECONNREFUSED 127.0.0.1:8799?token=${SECRET_TOKEN}`);
  err.code = "ECONNREFUSED";
  const payload = sanitizedStreamConnError(err);
  assert.equal(payload.code, "ECONNREFUSED");
  assert.ok(!JSON.stringify(payload).includes(SECRET_TOKEN));
});
