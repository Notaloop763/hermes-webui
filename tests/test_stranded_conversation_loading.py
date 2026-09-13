"""Node-harness behavior tests for stranded conversation loading settlement.

The in-flight latch helpers (``_conversationLoadingAgeMs`` /
``_sessionLoadInFlightFor``) and the escape hatch
(``_settleStrandedConversationLoading``) are extracted verbatim from
``static/sessions.js`` and exercised under node with hand-rolled DOM shims
(no JSDOM). Every assertion is on observable behavior — helper return values,
innerHTML writes, and retry-click dispatch — plus two narrow source-structure
pins where a behavior harness cannot reach production wiring (arm deadline,
force-reload re-arm).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Return the full function source for ``name`` from a single js file.

    Brace-depth tracking handles nested blocks and avoids fragile substring
    matching in the large, hand-formatted source file.
    """
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found in sessions.js"

    brace_start = source.find("{", start)
    assert brace_start >= 0, f"function {name} is missing '{{'"

    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False

    for index in range(brace_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ('\'', '"', "`"):
            in_string = ch
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]

    raise AssertionError(f"Could not extract function {name}")


def _extract_const(source: str, name: str) -> str:
    """Return the full ``const name = ...;`` declaration from a single js file."""
    m = re.search(rf"const {name}\s*=\s*[^;]+;", source)
    assert m, f"{name} not found in sessions.js"
    return m.group(0)


SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC = _extract_const(
    SESSIONS_SRC, "_SESSION_LOAD_IN_FLIGHT_MAX_MS"
)
CONVERSATION_LOADING_AGE_MS_SRC = _extract_function(
    SESSIONS_SRC, "_conversationLoadingAgeMs"
)
SESSION_LOAD_IN_FLIGHT_FOR_SRC = _extract_function(
    SESSIONS_SRC, "_sessionLoadInFlightFor"
)
SETTLE_STRANDED_CONVERSATION_LOADING_SRC = _extract_function(
    SESSIONS_SRC, "_settleStrandedConversationLoading"
)
ARM_STRANDED_TIMER_SRC = _extract_function(
    SESSIONS_SRC, "_armStrandedConversationLoadingTimer"
)
LOAD_SESSION_SRC = _extract_function(SESSIONS_SRC, "loadSession")


_NODE_SCRIPT = r'''
const M = { loadCalls: [] };

function makeMsgInner({ text, stamp }) {
  const el = {
    dataset: {},
    _innerHTML: '',
    _textContent: String(text),
    _retryHandler: null,
  };
  if (stamp !== undefined && stamp !== null) {
    el.dataset.conversationLoadingSince = String(stamp);
  }
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) { this._innerHTML = String(v); this._textContent = ''; },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._textContent; },
    set(v) { this._textContent = String(v); this._innerHTML = ''; },
  });
  el.querySelector = (sel) => {
    if (sel === '#conversationLoadRetry') {
      return {
        addEventListener: (type, fn) => { if (type === 'click') el._retryHandler = fn; },
      };
    }
    return null;
  };
  return el;
}

function installEnv({ text, stamp, loadingSid, sessionId }) {
  const msgInner = makeMsgInner({ text, stamp });
  globalThis.$ = (id) => (id === 'msgInner' ? msgInner : null);
  globalThis._loadingSessionId = loadingSid;
  globalThis.S = { session: sessionId === null ? null : { session_id: sessionId } };
  globalThis.loadSession = (sid, opts) => { M.loadCalls.push({ sid, opts }); };
  return msgInner;
}

__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__
__CONVERSATION_LOADING_AGE_MS_SRC__
__SESSION_LOAD_IN_FLIGHT_FOR_SRC__
__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__

function runLatchScenarios() {
  const results = {};
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-a', sessionId: 'sid-other' });
    results.fresh = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other' });
    results.stale = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-b', sessionId: 'sid-other' });
    results.otherSid = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: null, sessionId: 'sid-other' });
    results.nullSid = _sessionLoadInFlightFor('sid-a');
  }
  return results;
}

function runSettleScenario({ text, stamp, loadingSid, sessionId, settleSid, expectedStamp }) {
  M.loadCalls.length = 0;
  const inner = installEnv({ text, stamp, loadingSid, sessionId });
  // The placeholder's live stamp is set by installEnv via makeMsgInner when
  // `stamp` is provided. The timer path passes expectedStamp separately —
  // do NOT overwrite the dataset with it; the whole point of the
  // stale-timer guard is that expectedStamp may differ from the live stamp.
  _settleStrandedConversationLoading(settleSid, expectedStamp);
  const wroteRetry = inner._innerHTML.indexOf('conversationLoadRetry') !== -1;
  if (inner._retryHandler) inner._retryHandler();
  return {
    wroteRetry,
    textContentAfter: inner._textContent,
    loadCalls: M.loadCalls.slice(),
  };
}

const results = {
  latch: runLatchScenarios(),
  settle: {
    clearedLatch: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: null, sessionId: 'sid-other', settleSid: 'sid-a' }),
    staleLatch: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a' }),
    freshInflight: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a' }),
    sameSession: runSettleScenario({ text: 'Loading conversation...', stamp: null, loadingSid: null, sessionId: 'sid-a', settleSid: 'sid-a' }),
    noLoadingText: runSettleScenario({ text: 'Already rendered transcript', stamp: null, loadingSid: null, sessionId: 'sid-other', settleSid: 'sid-a' }),
    // Overlapping-timer race: A stamps live t1, B re-stamps the placeholder to a
    // newer stamp and owns the latch. A's timer fires with expectedStamp=t0
    // (stale, the original stamp A wrote), which now differs from the live t1
    // B overwrote → stamp guard bails, pane stays untouched (still B's Loading text).
    staleTimerSuperseded: runSettleScenario({ text: 'Loading conversation...', stamp: 1100, loadingSid: 'sid-b', sessionId: 'sid-other', settleSid: 'sid-a', expectedStamp: 1000 }),
    // Current-load timer fires with expectedStamp matching the live stamp, and the
    // load is stranded (past max-age / no live latch) → Retry must be written.
    liveTimerWritesRetry: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a', expectedStamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000) }),
  },
};

console.log(JSON.stringify(results));
'''


def _build_script() -> str:
    return (
        _NODE_SCRIPT.replace(
            "__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__", SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC
        )
        .replace("__CONVERSATION_LOADING_AGE_MS_SRC__", CONVERSATION_LOADING_AGE_MS_SRC)
        .replace("__SESSION_LOAD_IN_FLIGHT_FOR_SRC__", SESSION_LOAD_IN_FLIGHT_FOR_SRC)
        .replace(
            "__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__",
            SETTLE_STRANDED_CONVERSATION_LOADING_SRC,
        )
    )


def _run_node(script: str) -> dict:
    assert NODE is not None, "node is required"
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, (
        f"node produced no parseable output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return json.loads(output_lines[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_fresh_latch_reports_in_flight():
    body = _run_node(_build_script())
    assert body["latch"]["fresh"] is True, (
        "a fresh conversationLoadingSince stamp for the loading session must "
        "report the latch as in-flight"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_latch_expires():
    body = _run_node(_build_script())
    assert body["latch"]["stale"] is False, (
        "a stamp older than _SESSION_LOAD_IN_FLIGHT_MAX_MS must expire the latch"
    )
    assert body["latch"]["otherSid"] is False, (
        "a fresh stamp must not report in-flight when _loadingSessionId points "
        "at a different session"
    )
    assert body["latch"]["nullSid"] is False, (
        "a fresh stamp must not report in-flight when _loadingSessionId is null"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settle_writes_retry_only_when_stranded():
    body = _run_node(_build_script())
    settle = body["settle"]

    for label in ("clearedLatch", "staleLatch"):
        case = settle[label]
        assert case["wroteRetry"] is True, (
            f"{label}: no load owns the pane, so the retry escape hatch must be written"
        )
        assert case["loadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
            f"{label}: clicking Retry must call loadSession(sid, {{force: true}})"
        )

    for label in ("freshInflight", "sameSession", "noLoadingText"):
        case = settle[label]
        assert case["wroteRetry"] is False, (
            f"{label}: the pane must be left untouched (no retry written)"
        )
        assert case["loadCalls"] == [], (
            f"{label}: no Retry handler may be installed"
        )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_overlapping_timer_does_not_clobber_newer_load():
    """A's timer fires with a stale stamp (B re-stamped the placeholder) → pane untouched."""
    body = _run_node(_build_script())
    case = body["settle"]["staleTimerSuperseded"]
    assert case["wroteRetry"] is False, (
        "a stale expectedStamp must not overwrite a newer load's placeholder"
    )
    assert case["textContentAfter"].find("Loading conversation") != -1, (
        "the pane must still show the newer load's Loading text"
    )
    assert case["loadCalls"] == [], (
        "Retry must not fire for a superseded timer"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_timer_with_matching_stamp_writes_retry():
    """B's timer fires with expectedStamp matching the live stamp → stranded → Retry written."""
    body = _run_node(_build_script())
    case = body["settle"]["liveTimerWritesRetry"]
    assert case["wroteRetry"] is True, (
        "a current-load timer whose stamp still matches must write Retry"
    )
    assert case["loadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
        "clicking Retry must call loadSession(sid, {force: true})"
    )


# ── Fake-clock tests: drive the REAL scheduled callback (#7553 review) ───────
# The scenarios above call _settleStrandedConversationLoading directly with
# crafted stamps, which only proves settlement *after* expiration — not that
# production ever invokes settlement at the right time. These drive the real
# callback captured from _armStrandedConversationLoadingTimer under a fake
# clock: no Retry at 4s, Retry at the expiry boundary, stale callbacks leave
# newer placeholders untouched, and metadata-without-messages still settles.

_NODE_TIMER_SCRIPT = r'''
const M2 = { loadCalls: [] };
let nowMs = 1000000;
Date.now = () => nowMs;
const pendingTimers = [];
let timerSeq = 0;
globalThis.setTimeout = (cb, ms) => {
  timerSeq += 1;
  pendingTimers.push({ id: timerSeq, cb, due: nowMs + Number(ms), delay: Number(ms) });
  return timerSeq;
};
globalThis.clearTimeout = (id) => {
  const i = pendingTimers.findIndex((t) => t.id === id);
  if (i >= 0) pendingTimers.splice(i, 1);
};
function advance(ms) {
  nowMs += ms;
  const fire = [];
  for (let i = pendingTimers.length - 1; i >= 0; i--) {
    if (pendingTimers[i].due <= nowMs) fire.push(pendingTimers.splice(i, 1)[0]);
  }
  fire.sort((a, b) => a.due - b.due).forEach((t) => t.cb());
}

function makeTimerInner(stamp) {
  const el = {
    dataset: { conversationLoadingSince: String(stamp) },
    _html: '',
    _text: 'Loading conversation...',
    _retry: null,
    querySelector(sel) {
      if (sel === '#conversationLoadRetry') {
        return { addEventListener: (t, f) => { if (t === 'click') el._retry = f; } };
      }
      return null;
    },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      if (this._html.indexOf('conversationLoadRetry') !== -1) this._text = '';
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); },
  });
  return el;
}

let timerInner = null;
function installTimerEnv({ stamp, loadingSid, generation, sessionId, messages }) {
  timerInner = makeTimerInner(stamp);
  globalThis.$ = (id) => (id === 'msgInner' ? timerInner : null);
  globalThis._loadingSessionId = loadingSid;
  globalThis._loadSessionGeneration = generation;
  globalThis.S = { session: sessionId === null ? null : { session_id: sessionId } };
  if (messages !== undefined) globalThis.S.messages = messages;
  globalThis.loadSession = (sid, opts) => { M2.loadCalls.push({ sid, opts }); };
  M2.loadCalls.length = 0;
  pendingTimers.length = 0;
}

__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__
__CONVERSATION_LOADING_AGE_MS_SRC__
__SESSION_LOAD_IN_FLIGHT_FOR_SRC__
__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__
__ARM_STRANDED_TIMER_SRC__

function retryWritten() {
  return timerInner._html.indexOf('conversationLoadRetry') !== -1;
}

const T0 = 1000000;
const timerResults = {};

// A: the arm path schedules the real callback at the expiry deadline.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  timerResults.armedDelay = pendingTimers.length ? pendingTimers[pendingTimers.length - 1].delay : null;
}

// B: no Retry at 4s; Retry appears at the expiry boundary via the real callback.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(4000);
  timerResults.noRetryAt4s = !retryWritten();
  advance(16000);
  timerResults.retryAtExpiry = retryWritten();
  if (timerInner._retry) timerInner._retry();
  timerResults.retryLoadCalls = M2.loadCalls.slice();
}

// C: a stale scheduled callback (lost clearTimeout) leaves the newer placeholder alone,
// while the newer load's own callback still settles at its expiry.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  const staleCb = pendingTimers[0].cb;
  // Load B takes over: re-stamps the placeholder, bumps the generation, owns the latch.
  timerInner.dataset.conversationLoadingSince = String(T0 + 5000);
  globalThis._loadingSessionId = 'sid-b';
  globalThis._loadSessionGeneration = 2;
  _armStrandedConversationLoadingTimer('sid-b', T0 + 5000, 2);
  staleCb();
  timerResults.staleUntouched = !retryWritten()
    && timerInner._text.indexOf('Loading conversation') !== -1;
  advance(25000);
  timerResults.newerSettlesAtOwnExpiry = retryWritten();
}

// D: metadata assigned (S.session matches) but the messages request never
// resolves — the exact gap a bare session_id check misreads as completion.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(20000);
  timerResults.metaWithoutMessagesSettles = retryWritten();
}

// E: metadata assigned AND a renderable transcript arrived → stand down.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [{ role: 'user', content: 'hi' }] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(20000);
  timerResults.metaWithTranscriptStandsDown = !retryWritten();
}

// F (timer mechanics only — the re-arm here is hand-simulated; production
// wiring is pinned by test_same_session_force_reload_rearm_pinned_in_load_session):
// given a re-stamped + re-armed same-session reload, the original timer stands
// down at its expiry (stamp+generation mismatch) and the new timer settles.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  // Same-session force reload at T0+5000: re-stamp, bump generation, re-arm.
  nowMs = T0 + 5000;
  timerInner.dataset.conversationLoadingSince = String(T0 + 5000);
  globalThis._loadSessionGeneration = 2;
  _armStrandedConversationLoadingTimer('sid-a', T0 + 5000, 2);
  // Advance past the original expiry (T0+20000): original callback stands down.
  advance(15000); // nowMs = T0+20000
  timerResults.sameSessionForceReloadOriginalStandsDown = !retryWritten();
  // Advance to the new expiry (T0+25000): new callback settles → Retry.
  advance(5000); // nowMs = T0+25000
  timerResults.sameSessionForceReloadNewSettles = retryWritten();
}

console.log(JSON.stringify(timerResults));
'''


def _build_timer_script() -> str:
    return (
        _NODE_TIMER_SCRIPT.replace(
            "__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__", SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC
        )
        .replace("__CONVERSATION_LOADING_AGE_MS_SRC__", CONVERSATION_LOADING_AGE_MS_SRC)
        .replace("__SESSION_LOAD_IN_FLIGHT_FOR_SRC__", SESSION_LOAD_IN_FLIGHT_FOR_SRC)
        .replace(
            "__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__",
            SETTLE_STRANDED_CONVERSATION_LOADING_SRC,
        )
        .replace("__ARM_STRANDED_TIMER_SRC__", ARM_STRANDED_TIMER_SRC)
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_expiry_timer_armed_at_deadline():
    """The arm path must schedule the real callback at the expiry deadline.

    Guards the reviewed flaw: a 4s callback racing the 20s latch can never
    settle, so production must arm at _SESSION_LOAD_IN_FLIGHT_MAX_MS.
    """
    assert "_SESSION_LOAD_IN_FLIGHT_MAX_MS" in ARM_STRANDED_TIMER_SRC, (
        "the arm helper must schedule at the expiry deadline"
    )
    assert "4000" not in ARM_STRANDED_TIMER_SRC, (
        "the arm helper must not use a separate early callback"
    )
    body = _run_node(_build_timer_script())
    assert body["armedDelay"] == 20000, (
        "the real scheduled callback must fire at the 20s expiry deadline"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_retry_before_expiry_retry_at_expiry():
    """Drive the real scheduled callback: silent at 4s, Retry at 20s."""
    body = _run_node(_build_timer_script())
    assert body["noRetryAt4s"] is True, "no Retry may appear before the deadline"
    assert body["retryAtExpiry"] is True, "Retry must appear at the expiry boundary"
    assert body["retryLoadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
        "clicking Retry must call loadSession(sid, {force: true})"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_scheduled_callback_leaves_newer_placeholder():
    """A superseded load's captured callback must not settle the newer load."""
    body = _run_node(_build_timer_script())
    assert body["staleUntouched"] is True, (
        "a stale stamp+generation must leave the newer placeholder untouched"
    )
    assert body["newerSettlesAtOwnExpiry"] is True, (
        "the newer load's own callback must still settle at its expiry"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_metadata_without_messages_still_settles():
    """S.session assigned but messages never resolve → still Retry.

    A bare S.session.session_id check misreads this gap as completion;
    placeholder text plus ownership stamp plus no transcript must settle.
    """
    body = _run_node(_build_timer_script())
    assert body["metaWithoutMessagesSettles"] is True, (
        "metadata without a renderable transcript must still settle to Retry"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_metadata_with_transcript_stands_down():
    """S.session assigned AND a renderable transcript arrived → no Retry."""
    body = _run_node(_build_timer_script())
    assert body["metaWithTranscriptStandsDown"] is True, (
        "an arrived transcript must stand the settle down"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_force_reload_rearm_pinned_in_load_session():
    """loadSession must re-stamp + re-arm the timer on same-session force reload.

    Source-structure pin: the mechanics test below hand-simulates the re-arm, so
    it cannot prove production performs it. This pins the production branch
    itself — a ``sameSessionForceReload``-gated re-stamp of
    ``conversationLoadingSince`` plus an ``_armStrandedConversationLoadingTimer``
    call with the new generation, conditioned on the placeholder still showing
    "Loading conversation" (so Retry/rendered panes never arm a timer).
    """
    m = re.search(
        r"if\s*\(\s*sameSessionForceReload\b(.*?)\{\s*"
        r"const loadingStamp\s*=\s*Date\.now\(\);\s*"
        r"_msgInner\.dataset\.conversationLoadingSince\s*=\s*String\(loadingStamp\);\s*"
        r"_armStrandedConversationLoadingTimer\(sid,\s*loadingStamp,\s*_loadGeneration\);",
        LOAD_SESSION_SRC,
        re.S,
    )
    assert m, (
        "loadSession must re-stamp + re-arm the expiry timer on a same-session "
        "force reload (forced reload otherwise loses its only timer)"
    )
    assert "Loading conversation" in m.group(1), (
        "the re-arm must be conditioned on the Loading placeholder text so "
        "Retry/rendered panes never arm a timer"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_force_reload_timer_mechanics():
    """Timer mechanics given a re-armed same-session reload (hand-simulated).

    The original timer stands down at its expiry (stamp+generation mismatch
    from the re-stamped, re-armed same-session reload), and the new timer
    settles at its own expiry → Retry appears. This drives the extracted timer
    helpers only; the production re-arm branch itself is pinned by
    test_same_session_force_reload_rearm_pinned_in_load_session.
    """
    body = _run_node(_build_timer_script())
    assert body["sameSessionForceReloadOriginalStandsDown"] is True, (
        "the original timer must stand down at its expiry (stamp+generation "
        "mismatch from the re-stamped, re-armed same-session reload)"
    )
    assert body["sameSessionForceReloadNewSettles"] is True, (
        "the re-armed timer must settle at its own expiry → Retry appears"
    )
