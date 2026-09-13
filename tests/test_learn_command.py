"""Tests for POST /api/learn and the /learn slash command.

Modeled on tests/test_1062_busy_input_modes.py (frontend source assertions) and
tests/test_commands_endpoint.py (backend POST via TEST_BASE + requires_agent_modules).
"""
import json
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from tests.conftest import TEST_BASE, requires_agent_modules

ROOT = Path(__file__).parent.parent
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


# ── Frontend: slash command registration ──────────────────────────────────────

class TestLearnCommandRegistration:
    """The /learn slash command must be wired into commands.js + i18n.js."""

    def test_learn_in_commands_array(self):
        assert "{name:'learn'" in COMMANDS_JS and "fn:cmdLearn" in COMMANDS_JS

    def test_cmd_learn_handler_present(self):
        assert "async function cmdLearn(args)" in COMMANDS_JS

    def test_learn_handler_alias_registered(self):
        assert "HANDLERS.learn = cmdLearn" in COMMANDS_JS

    def test_i18n_cmd_learn_en_locale(self):
        en_idx = I18N_JS.find("en: {")
        ns_idx = I18N_JS.find("cmd_learn:", en_idx)
        assert ns_idx > en_idx, "cmd_learn key missing from en locale block"


# Source-level presence assertions: these documents the exact strings the patch
# adds. They pass once the patch is applied (and would fail on clean master).
