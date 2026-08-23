const test = require("node:test");
const assert = require("node:assert/strict");

const { scrollToFeedEnd } = require("./feed-scroll.cjs");

/**
 * Electron-side feed scroll contract (jsdom layout tests live in feedScroll.test.ts).
 * Repro harness: pinned tail + growth should land at scrollHeight - clientHeight.
 */
test("scrollToEnd contract matches Marionette feedScroll helper", () => {
  assert.equal(scrollToFeedEnd(2000, 400), 1600);
  assert.equal(scrollToFeedEnd(350, 400), 0);
});

test("stream-to-fold live tail growth outside the virtual window still pins to the true end", () => {
  const client = 400;
  const virtualHeadHeight = 1600;
  const initialTail = 80;
  const pinnedTop = scrollToFeedEnd(virtualHeadHeight + initialTail, client);
  const grownTail = 320;
  const nextTop = scrollToFeedEnd(virtualHeadHeight + grownTail, client);
  assert.equal(nextTop, pinnedTop + (grownTail - initialTail));
});
