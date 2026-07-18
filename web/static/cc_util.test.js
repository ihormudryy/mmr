/* Pure-logic tests for cc_util.js — run with `node cc_util.test.js`.
 * No DOM, no harness: exit 0 = pass, exit 1 = failure (assertion prints).
 * A pytest wrapper (tests/test_cc_util_js.py) runs this and skips if node
 * is unavailable. */
'use strict';

const assert = require('node:assert/strict');
const U = require('./cc_util.js');

let n = 0;
function test(name, fn) { fn(); n += 1; }

/* ---- ccServerClockOffsetMs ---- */
test('offset is client-minus-server from generated_at', () => {
  // server said it generated the snapshot at t=1000s; client clock reads 1005s.
  assert.equal(U.ccServerClockOffsetMs('1970-01-01T00:16:40+00:00', 1005_000), 5_000);
});
test('offset is 0 for missing/unparsable generated_at', () => {
  assert.equal(U.ccServerClockOffsetMs(null, 123), 0);
  assert.equal(U.ccServerClockOffsetMs('not-a-date', 123), 0);
});

/* ---- ccQuoteAgeSeconds ---- */
test('null when quote or timestamp missing (treated as stale by caller)', () => {
  assert.equal(U.ccQuoteAgeSeconds(null, 0, 1000), null);
  assert.equal(U.ccQuoteAgeSeconds({ last: 1 }, 0, 1000), null);
  assert.equal(U.ccQuoteAgeSeconds({ server_received_timestamp: 'nope' }, 0, 1000), null);
});
test('age is skew-corrected: quote 45s old at snapshot still reads ~45s, not 0', () => {
  // Server clock is 10s BEHIND the client. Snapshot generated_at (server) maps
  // to a +10000ms offset. A quote the server received 45s before it generated
  // the snapshot must read ~45s old immediately, regardless of client arrival.
  const genServerIso = '2026-07-15T12:00:00+00:00';
  const genServerMs = Date.parse(genServerIso);
  const clientNowAtSnapshot = genServerMs + 10_000; // client 10s ahead of server
  const offset = U.ccServerClockOffsetMs(genServerIso, clientNowAtSnapshot);
  assert.equal(offset, 10_000);
  const quote = { server_received_timestamp: new Date(genServerMs - 45_000).toISOString() };
  // Render happens at the same instant as the snapshot for this check.
  const age = U.ccQuoteAgeSeconds(quote, offset, clientNowAtSnapshot);
  assert.ok(Math.abs(age - 45) < 0.01, `expected ~45s, got ${age}`);
});
test('age keeps advancing on the client clock after the snapshot', () => {
  const genIso = '2026-07-15T12:00:00+00:00';
  const genMs = Date.parse(genIso);
  const offset = U.ccServerClockOffsetMs(genIso, genMs); // clocks aligned -> 0
  const quote = { server_received_timestamp: genIso };
  // 12s later on the client clock:
  assert.ok(Math.abs(U.ccQuoteAgeSeconds(quote, offset, genMs + 12_000) - 12) < 0.01);
});
test('small clock overshoot clamps to 0 (fresh), never negative', () => {
  const quote = { server_received_timestamp: '2026-07-15T12:00:00+00:00' };
  const rMs = Date.parse(quote.server_received_timestamp);
  assert.equal(U.ccQuoteAgeSeconds(quote, 0, rMs - 500), 0); // "future" quote -> 0
});

/* ---- ccIsDegraded ---- */
test('degraded when bridge lifecycle is not live, even if SSE is open', () => {
  const sseOpen = { open: true, disconnectedForMs: null, degradedAfterMs: 15000, polling: false };
  assert.equal(U.ccIsDegraded('degraded', sseOpen), true);
  assert.equal(U.ccIsDegraded('disconnected', sseOpen), true);
  assert.equal(U.ccIsDegraded('live', sseOpen), false);
});
test('degraded when SSE is down past the grace window, even if lifecycle live', () => {
  assert.equal(U.ccIsDegraded('live',
    { open: false, disconnectedForMs: 20000, degradedAfterMs: 15000, polling: false }), true);
  assert.equal(U.ccIsDegraded('live',
    { open: false, disconnectedForMs: 3000, degradedAfterMs: 15000, polling: false }), false);
});
test('degraded while polling', () => {
  assert.equal(U.ccIsDegraded('live',
    { open: false, disconnectedForMs: null, degradedAfterMs: 15000, polling: true }), true);
});

/* ---- ccSnapshotSupersedes ---- */
test('a new fenced stream always supersedes', () => {
  assert.equal(U.ccSnapshotSupersedes('stream-a', 99, { stream_id: 'stream-b', sequence: 0 }), true);
});
test('same stream: only a newer-or-equal sequence supersedes (rejects rollback)', () => {
  assert.equal(U.ccSnapshotSupersedes('s', 50, { stream_id: 's', sequence: 51 }), true);
  assert.equal(U.ccSnapshotSupersedes('s', 50, { stream_id: 's', sequence: 50 }), true);
  assert.equal(U.ccSnapshotSupersedes('s', 50, { stream_id: 's', sequence: 49 }), false);
});
test('a malformed view never supersedes', () => {
  assert.equal(U.ccSnapshotSupersedes('s', 1, null), false);
  assert.equal(U.ccSnapshotSupersedes('s', 1, {}), false);
});

console.log(`cc_util.test.js: ${n} tests passed`);
