"use strict";

/** Authoritative scrollTop for stick-to-bottom (shared with feedScroll.ts). */
function scrollToFeedEnd(scrollHeight, clientHeight) {
  return Math.max(0, scrollHeight - clientHeight);
}

module.exports = { scrollToFeedEnd };
