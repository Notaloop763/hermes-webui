"""Regression coverage for session-owned composer reasoning effort."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import api.models as models
from api.config import resolve_session_reasoning_effort
from api.gateway_chat import _gateway_reasoning_effort_for_request
from api.models import Session
from api.routes import handle_get, handle_post


REPO = Path(__file__).resolve().parents[1]


class _DummyHandler:
    client_address = ("127.0.0.1", 12345)

    def __init__(self, body: dict | None = None, *, command: str = "GET"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.command = command
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = tempfile.SpooledTemporaryFile()
        self.rfile.write(raw)
        self.rfile.seek(0)
        self.wfile = tempfile.SpooledTemporaryFile()
        self.status = None

    def send_response(self, code: int):
        self.status = code

    def send_header(self, _key: str, _value: str):
        pass

    def end_headers(self):
        pass

    def payload(self) -> dict:
        self.wfile.seek(0)
        return json.loads(self.wfile.read().decode("utf-8"))


def test_session_projection_carries_reasoning_effort():
    session = Session(model="gpt-5", model_provider="openai", reasoning_effort="high")
    assert session.compact()["reasoning_effort"] == "high"


def test_session_reasoning_effort_survives_save_reload(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    session = Session(
        session_id="reasoning-session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="xhigh",
    )
    session.save(touch_updated_at=False)

    assert Session.load("reasoning-session").reasoning_effort == "xhigh"


def test_new_session_snapshots_profile_reasoning_effort(tmp_path):
    with patch("api.models._profile_default_reasoning_effort", return_value="high"):
        session = models.new_session(workspace=str(tmp_path), profile="work")

    try:
        assert session.reasoning_effort == "high"
    finally:
        models.SESSIONS.pop(session.session_id, None)


def test_session_effort_is_authoritative_and_legacy_sessions_use_profile_default():
    cfg = {"agent": {"reasoning_effort": "high"}}
    assert resolve_session_reasoning_effort(cfg, session_effort="low") == "low"
    assert resolve_session_reasoning_effort(cfg, session_effort="") == ""
    assert resolve_session_reasoning_effort(cfg, session_effort=None) == "high"
    assert _gateway_reasoning_effort_for_request(cfg, session_effort="low") == "low"


def test_reasoning_get_uses_session_override():
    session = SimpleNamespace(reasoning_effort="low")
    captured = {}

    def status(**kwargs):
        captured.update(kwargs)
        return {"reasoning_effort": kwargs.get("effort_override", "high")}

    handler = _DummyHandler()
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch("api.routes.get_session", return_value=session),
        patch("api.routes.get_reasoning_status", side_effect=status),
    ):
        handle_get(
            handler,
            urlparse("/api/reasoning?model=gpt-5&provider=openai&session_id=session-b"),
        )

    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "low"
    assert captured["effort_override"] == "low"


def test_reasoning_get_rejects_session_outside_active_profile():
    handler = _DummyHandler()

    def reject(other_handler, _session_id):
        other_handler.send_response(409)
        other_handler.end_headers()
        other_handler.wfile.write(
            json.dumps({"error": "Session belongs to a different profile"}).encode()
        )
        return False

    with (
        patch("api.routes._session_id_visible_to_request_profile", side_effect=reject),
        patch("api.routes.get_reasoning_status") as status,
    ):
        handle_get(handler, urlparse("/api/reasoning?session_id=other-profile"))

    assert handler.status == 409
    assert handler.payload()["error"] == "Session belongs to a different profile"
    status.assert_not_called()


def test_reasoning_post_persists_session_override_and_evicts_cached_agent():
    events = []

    class _MutationLock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_exc):
            events.append("lock-exit")

    session = SimpleNamespace(
        reasoning_effort="high", save=lambda: events.append("save")
    )
    evicted = []
    handler = _DummyHandler(
        {
            "effort": "low",
            "model": "gpt-5",
            "provider": "openai",
            "session_id": "session-b",
        },
        command="POST",
    )
    with (
        patch("api.routes._get_or_materialize_session", return_value=session),
        patch("api.routes._get_session_agent_lock", return_value=_MutationLock()),
        patch(
            "api.routes.set_reasoning_effort",
            return_value={"reasoning_effort": "low", "supported_efforts": ["low", "high"]},
        ),
        patch(
            "api.config._evict_session_agent",
            side_effect=lambda session_id: (
                evicted.append(session_id),
                events.append("evict"),
            ),
        ),
    ):
        handle_post(handler, urlparse("/api/reasoning"))

    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "low"
    assert session.reasoning_effort == "low"
    assert evicted == ["session-b"]
    assert events == ["lock-enter", "save", "lock-exit", "evict"]


def test_reasoning_post_does_not_mutate_profile_for_unknown_session():
    handler = _DummyHandler(
        {"effort": "low", "session_id": "missing"}, command="POST"
    )
    with (
        patch("api.routes._get_or_materialize_session", side_effect=KeyError),
        patch("api.routes.set_reasoning_effort") as set_effort,
    ):
        handle_post(handler, urlparse("/api/reasoning"))

    assert handler.status == 404
    assert handler.payload()["error"] == "Session not found"
    set_effort.assert_not_called()


def test_runtime_paths_prefer_session_reasoning_effort():
    streaming = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    gateway = (REPO / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert "resolve_session_reasoning_effort(" in streaming
    assert "session_effort=getattr(_session_meta, 'reasoning_effort', None)" in streaming
    assert "resolve_session_reasoning_effort(" in gateway
    assert 'session_effort=getattr(s, "reasoning_effort", None)' in gateway


def test_reasoning_slash_command_posts_active_session_context():
    commands = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
    start = commands.index("function cmdReasoning")
    end = commands.index("function cmdVoice", start)
    body = commands[start:end]
    assert "_reasoningEffortContext()" in body
