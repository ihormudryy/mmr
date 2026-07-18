/* Pure, DOM-free helpers for the command center client.
 *
 * Loaded (defer) BEFORE command_center.js so the browser gets these as
 * globals, and require()-able in node for unit tests (cc_util.test.js). No
 * document / window / fetch / EventSource access may appear in this file — it
 * exists precisely so the correctness-critical, side-effect-free logic
 * (quote-freshness math, degraded predicate, snapshot-supersede guard) can be
 * tested in isolation. The stateful glue stays in command_center.js. */
'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
}(typeof self !== 'undefined' ? self : this, function () {

  /* Offset between the client clock and the server clock, learned from a
   * snapshot's server-stamped `generated_at`. Positive when the client is
   * ahead of the server. Applying this offset to server timestamps lets the
   * client age a quote WITHOUT depending on the two clocks being in sync.
   * Returns 0 (no correction) when generated_at is missing/unparsable. */
  function ccServerClockOffsetMs(generatedAtIso, nowMs) {
    if (!generatedAtIso) return 0;
    const genMs = Date.parse(generatedAtIso);
    return Number.isNaN(genMs) ? 0 : (nowMs - genMs);
  }

  /* Age of a quote in seconds, from its server-stamped receive time corrected
   * onto the client clock via `offsetMs`. Returns null when it can't be told
   * (missing or unparsable timestamp) — the caller MUST treat null as stale,
   * never as fresh. This is what makes a stale quote read stale immediately
   * after a snapshot/reconnect instead of being re-stamped "0s". */
  function ccQuoteAgeSeconds(quote, offsetMs, nowMs) {
    if (!quote || !quote.server_received_timestamp) return null;
    const receivedMs = Date.parse(quote.server_received_timestamp);
    if (Number.isNaN(receivedMs)) return null;
    const offset = Number.isFinite(offsetMs) ? offsetMs : 0;
    const age = (nowMs - offset - receivedMs) / 1000;
    return age < 0 ? 0 : age;  // clamp small clock overshoot to "fresh"
  }

  /* Whether the degraded banner should be shown. True when the bridge is not
   * LIVE (the server can't get fresh data from trader_service) OR the SSE
   * transport is down past the grace window / actively polling (the client
   * can't receive). The bridge lifecycle is authoritative and independent of
   * the EventSource merely being "open" — an open SSE to the dashboard process
   * says nothing about whether that process still has live trader data. */
  function ccIsDegraded(lifecycle, sse) {
    const healthBad = !!lifecycle && lifecycle !== 'live';
    const transportBad = !!sse && (
      sse.polling === true ||
      (sse.open === false && sse.disconnectedForMs !== null
        && sse.disconnectedForMs >= sse.degradedAfterMs));
    return healthBad || transportBad;
  }

  /* Guard against a late/overlapping snapshot rolling state backward: a slow
   * older fetch resolving after a newer one has already been applied. A
   * snapshot on a NEW stream is always accepted (a resync rotated the stream
   * id, so its sequence space is unrelated). On the SAME stream, only a
   * sequence >= the applied one is accepted. A malformed view never
   * supersedes. */
  function ccSnapshotSupersedes(appliedStreamId, appliedSequence, view) {
    if (!view || !view.stream_id) return false;
    if (view.stream_id !== appliedStreamId) return true;
    return (view.sequence || 0) >= (appliedSequence || 0);
  }

  return {
    ccServerClockOffsetMs,
    ccQuoteAgeSeconds,
    ccIsDegraded,
    ccSnapshotSupersedes,
  };
}));
