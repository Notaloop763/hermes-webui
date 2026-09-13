"""Tests for POST /api/learn and the /learn slash command.

Backend: real HTTP POSTs via TEST_BASE (gated by requires_agent_modules).
Frontend: behavioral tests that execute the REAL static/commands.js in node's
vm with mocked browser globals (following tests/test_cli_only_slash_commands.py
and tests/test_composer_capture_clear_race.py). No source-substring asserts:
each test invokes the live dispatcher/handler and checks what it DID.
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import TEST_BASE, requires_agent_modules

ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# ── Backend: POST /api/learn route ────────────────────────────────────────────


@requires_agent_modules
def test_learn_endpoint_builds_prompt_from_request():
    payload = json.dumps({"request": "the release checklist workflow"}).encode()
    req = urllib.request.Request(
        f"{TEST_BASE}/api/learn", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    assert body["prompt"].startswith("[/learn]")
    assert "the release checklist workflow" in body["prompt"]


@requires_agent_modules
def test_learn_endpoint_empty_request_learns_from_conversation():
    payload = json.dumps({"request": ""}).encode()
    req = urllib.request.Request(
        f"{TEST_BASE}/api/learn", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    assert "this conversation" in body["prompt"]


# ── Frontend: behavioral harness (real commands.js in node vm) ─────────────────


def _node() -> str:
    exe = shutil.which("node")
    if not exe:  # pragma: no cover
        pytest.skip("node not available")
        raise RuntimeError("unreachable")
    return exe


def _run_learn_harness(api_js, test_js, composer_initial=""):
    """Load the REAL commands.js with mocked globals, run test_js, return its result.

    Mock browser state: S (session sid-1), $('msg') composer element backed by
    __state.composer, send()/showToast()/api() record into __sendCount/__toasts/
    __calls. api_js is harness-scope JS (may close over S/state); test_js runs
    INSIDE the vm and must read results via the __-prefixed ctx globals.
    Assumption (matches the real dispatcher): cmdLearn is invoked with the
    composer already cleared (static/messages.js send() runs the handler then
    synchronously sets $('msg').value='' without awaiting), so
    composer_initial="" is the post-dispatch state and any non-empty composer
    observed after the api await is a user draft typed mid-request.
    """
    node = _node()
    harness = (
        "const vm = require('vm');\n"
        "const __calls = [];\n"
        "const __toasts = [];\n"
        "const __sendCount = {n: 0};\n"
        "const __state = {composer: " + json.dumps(composer_initial) + "};\n"
        "const S = {session: {session_id: 'sid-1'}, pendingFiles: []};\n"
        "const composerEl = {};\n"
        "Object.defineProperty(composerEl, 'value', {"
        "get(){return __state.composer;}, set(v){__state.composer = v;}, configurable: true});\n"
        "async function __api(" + "path, opts" + ") {\n" + api_js + "\n}\n"
        "const ctx = {\n"
        "  console,\n"
        "  localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},\n"
        "  t: (key) => key,\n"
        "  S,\n"
        "  $: (id) => (id === 'msg' ? composerEl : null),\n"
        "  autoResize(){},\n"
        "  showToast(msg){ __toasts.push(String(msg)); },\n"
        "  send: async () => { __sendCount.n++; },\n"
        "  api: __api,\n"
        "  __calls, __toasts, __sendCount, __state,\n"
        "};\n"
        "vm.createContext(ctx);\n"
        "vm.runInContext(" + json.dumps(COMMANDS_JS) + ", ctx);\n"
        "(async () => {\n"
        "  const result = await vm.runInContext(\"(async () => { " + test_js + " })()\", ctx);\n"
        "  process.stdout.write(JSON.stringify(result));\n"
        "})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(harness)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


HAPPY_API_JS = (
    "__calls.push({path, body: JSON.parse(opts.body)});\n"
    "return {prompt: '[/learn] generated prompt'};"
)


def test_learn_dispatched_through_command_table():
    """/learn must resolve through the live dispatcher to cmdLearn (noEcho)."""
    result = _run_learn_harness(
        HAPPY_API_JS,
        "const entry = COMMANDS.find(c => c.name === 'learn');"
        " const parsed = parseCommand('/learn my request');"
        " return {found: !!entry, noEcho: !!(entry && entry.noEcho),"
        "  fnIsCmdLearn: !!(entry && entry.fn === cmdLearn),"
        "  handlerAlias: HANDLERS.learn === cmdLearn,"
        "  parsedName: parsed && parsed.name, parsedArgs: parsed && parsed.args};",
    )
    assert result == {
        "found": True,
        "noEcho": True,
        "fnIsCmdLearn": True,
        "handlerAlias": True,
        "parsedName": "learn",
        "parsedArgs": "my request",
    }


def test_cmd_learn_sends_prompt_through_chat_pipeline():
    """Happy path: POSTs the request, fills the composer, calls send()."""
    result = _run_learn_harness(
        HAPPY_API_JS,
        "await cmdLearn('my request');"
        " return {composer: __state.composer, sendCalls: __sendCount.n,"
        "  toasts: __toasts, apiCalls: __calls};",
    )
    assert result["apiCalls"] == [{"path": "/api/learn", "body": {"request": "my request"}}]
    assert result["composer"] == "[/learn] generated prompt"
    assert result["sendCalls"] == 1
    assert result["toasts"] == []


def test_cmd_learn_preserves_draft_typed_during_request():
    """Race guard (P1): a draft typed while /api/learn is pending must survive.

    Fails on the pre-fix handler, which unconditionally overwrote the composer
    and submitted the prompt, silently discarding the user's draft.
    """
    result = _run_learn_harness(
        "__calls.push({path});"
        " __state.composer = 'user draft typed during request';"
        " return {prompt: '[/learn] generated prompt'};",
        "await cmdLearn('my request');"
        " return {composer: __state.composer, sendCalls: __sendCount.n, toasts: __toasts};",
    )
    assert result["composer"] == "user draft typed during request"
    assert result["sendCalls"] == 0
    assert any("learn_composer_busy" in toast for toast in result["toasts"]), (
        "expected a composer-busy toast preserving the draft"
    )


def test_cmd_learn_aborts_when_session_changes_during_request():
    """The prompt must not be submitted into a different session than the caller's."""
    result = _run_learn_harness(
        "__calls.push({path});"
        " S.session.session_id = 'sid-2';"
        " return {prompt: '[/learn] generated prompt'};",
        "await cmdLearn('my request');"
        " return {composer: __state.composer, sendCalls: __sendCount.n, toasts: __toasts};",
    )
    assert result["composer"] == ""
    assert result["sendCalls"] == 0
    assert any("learn_session_changed" in toast for toast in result["toasts"]), (
        "a session switch must abort with the session-changed toast, not the composer-busy one"
    )


def test_cmd_learn_empty_prompt_shows_toast_without_sending():
    result = _run_learn_harness(
        "return {};",
        "await cmdLearn('my request');"
        " return {composer: __state.composer, sendCalls: __sendCount.n, toasts: __toasts};",
    )
    assert result["sendCalls"] == 0
    assert any("learn_no_prompt" in toast for toast in result["toasts"])


def test_cmd_learn_without_session_does_not_call_api():
    result = _run_learn_harness(
        HAPPY_API_JS,
        "S.session = null; await cmdLearn('my request');"
        " return {sendCalls: __sendCount.n, toasts: __toasts, apiCalls: __calls};",
    )
    assert result["apiCalls"] == []
    assert result["sendCalls"] == 0
    assert any("no_active_session" in toast for toast in result["toasts"])


def test_learn_i18n_keys_resolve_in_en_locale():
    """The /learn strings must resolve from the live LOCALES runtime object."""
    node = _node()
    script = textwrap.dedent(
        """
        const vm = require('vm');
        // i18n.js runs loadLocale() at the top level, which touches
        // localStorage + document.documentElement — stub both.
        const ctx = {
          console,
          localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},
          document: {documentElement: {}, querySelectorAll(){return [];}},
        };
        vm.createContext(ctx);
        vm.runInContext(%s, ctx);
        const out = vm.runInContext("({cmd_learn: LOCALES.en.cmd_learn, learn_failed: LOCALES.en.learn_failed, learn_no_prompt: LOCALES.en.learn_no_prompt, learn_composer_busy: LOCALES.en.learn_composer_busy, learn_session_changed: LOCALES.en.learn_session_changed, resolved: t('learn_composer_busy')})", ctx);
        process.stdout.write(JSON.stringify(out));
        """
    ) % json.dumps(I18N_JS)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run([node, str(script_path)], check=True, capture_output=True, text=True, timeout=60)
    finally:
        script_path.unlink(missing_ok=True)
    result = json.loads(proc.stdout)
    assert result["cmd_learn"], "LOCALES.en.cmd_learn must be non-empty"
    assert result["learn_failed"], "LOCALES.en.learn_failed must be non-empty"
    assert result["learn_no_prompt"], "LOCALES.en.learn_no_prompt must be non-empty"
    assert result["learn_composer_busy"], "LOCALES.en.learn_composer_busy must be non-empty"
    assert result["learn_session_changed"], "LOCALES.en.learn_session_changed must be non-empty"
    assert result["resolved"] == result["learn_composer_busy"], (
        "t('learn_composer_busy') must resolve through the live locale fallback chain"
    )
