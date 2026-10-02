"""Regression coverage for session-owned composer reasoning effort."""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
from api import profiles
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


@pytest.fixture
def isolated_reasoning_profiles(tmp_path, monkeypatch):
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "agent:\n  reasoning_effort: low\nwebui_gateway_base_url: http://root-gateway:8650\n"
    )
    (work / "config.yaml").write_text(
        "agent:\n  reasoning_effort: high\nwebui_gateway_base_url: http://work-gateway:8650\n"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(root / "config.yaml"))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    config.reload_config()
    return root, work


def test_new_session_defaults_honor_external_config_path(
    isolated_reasoning_profiles, tmp_path, monkeypatch
):
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(
        "model:\n  default: gpt-5\n  provider: openai\n"
        "agent:\n  reasoning_effort: high\n"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    session = models.new_session(workspace=str(tmp_path), profile="default")

    assert session.reasoning_effort == "high"
    assert session.model == "gpt-5"
    assert session.model_provider == "openai"
    # An external root config must not replace a different profile's defaults.
    other = models.new_session(workspace=str(tmp_path), model="gpt-5", profile="work")
    assert other.reasoning_effort == "high"
    external.write_text("agent:\n  reasoning_effort: low\n")
    config.reload_config()
    assert models.new_session(
        workspace=str(tmp_path), model="gpt-5", profile="work"
    ).reasoning_effort == "high"


@pytest.mark.parametrize("config_location", ["root", "external", "work", "work-nested"])
@pytest.mark.parametrize("supplied_model", [None, "gpt-5"])
def test_named_request_new_session_defaults_stay_in_profile(
    isolated_reasoning_profiles, tmp_path, monkeypatch, config_location, supplied_model
):
    root, work = isolated_reasoning_profiles
    (root / "config.yaml").write_text(
        "model:\n  default: gpt-5.5\n  provider: openai\nagent:\n  reasoning_effort: low\n"
    )
    (work / "config.yaml").write_text(
        "model:\n  default: gpt-5\n  provider: openai\nagent:\n  reasoning_effort: high\n"
    )
    override = {"root": root / "config.yaml", "external": tmp_path / "external.yaml",
                "work": work / "override.yaml",
                "work-nested": work / "mounted" / "override.yaml"}[config_location]
    override.parent.mkdir(parents=True, exist_ok=True)
    if config_location != "root":
        override.write_text(
            "model:\n  default: gpt-5\n  provider: openai\nagent:\n  reasoning_effort: xhigh\n"
            if config_location.startswith("work") else (root / "config.yaml").read_text()
        )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override))
    config.reload_config()
    profiles.set_request_profile("work")
    try:
        assert profiles.get_active_hermes_home() == work
        session = models.new_session(workspace=str(tmp_path), profile="work", model=supplied_model)
    finally:
        profiles.clear_request_profile()
    assert session.model == "gpt-5"
    if supplied_model is None:
        assert session.model_provider == "openai"
    expected = "xhigh" if config_location.startswith("work") else "high"
    assert session.reasoning_effort == expected
    # Detached workers must resolve the same default without request-local TLS.
    assert config.get_config_for_profile_home(work, isolate_config_override=True)["agent"]["reasoning_effort"] == expected


@pytest.mark.parametrize("http_error", [False, True], ids=["success", "auth-error"])
@pytest.mark.parametrize("work_key", ["work-test-key", ""], ids=["profile-key", "no-profile-key"])
@pytest.mark.parametrize("session_effort, expected", [(None, "high"), ("xhigh", "xhigh")])
@pytest.mark.parametrize("runs_api", [False, True], ids=["legacy-api", "runs-api"])
def test_gateway_worker_resolves_reasoning_from_session_profile(
    isolated_reasoning_profiles, tmp_path, monkeypatch, session_effort, expected, runs_api, work_key, http_error
):
    # Simulate a detached worker with root ambient config and a named-profile
    # legacy session. Exercise real config resolution and outbound JSON encoding.
    assert config.get_config()["agent"]["reasoning_effort"] == "low"
    session = Session(
        session_id="gateway-profile-effort", profile="work", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort=session_effort,
    )
    models.SESSIONS[session.session_id] = session
    stream_id = "gateway-profile-effort-stream"
    session.active_stream_id = stream_id
    session.pending_user_message = "hello"
    channel = config.create_stream_channel()
    events = channel.subscribe()
    monkeypatch.setitem(config.STREAMS, stream_id, channel)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1" if runs_api else "0")
    root, work = isolated_reasoning_profiles
    for name, home in (("root", root), ("work", work)):
        (home / ".env").write_text(
            f"HERMES_WEBUI_GATEWAY_BASE_URL=http://{name}-gateway:8650\n"
            f"HERMES_WEBUI_GATEWAY_API_KEY={work_key if name == 'work' else 'root-test-key'}\n"
        )
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "root-test-key")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {
        "HERMES_WEBUI_GATEWAY_API_KEY",
    })
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a: True)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *a: None)
    # Avoid external platform discovery in system-prompt preparation.
    monkeypatch.setattr("api.streaming._load_webui_prefill_context", lambda cfg: {})
    monkeypatch.setattr("api.streaming._webui_ephemeral_system_prompt", lambda *a, **k: "test")
    captured = []

    def urlopen(request, timeout=None):
        if request.get_method() == "POST":
            captured.append((request.full_url, request.get_header("Authorization"),
                             gateway_chat._STREAM_ENDPOINTS[stream_id], json.loads(request.data)))
            if http_error:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            if runs_api:
                return io.BytesIO(b'{"run_id":"test-run"}')
            return io.BytesIO(
                b'data: {"choices":[{"delta":{"content":"done"}}]}\n'
                b'data: [DONE]\n'
            )
        return io.BytesIO(
            b'data: {"event":"run.completed","output":"done"}\n'
            b'data: [DONE]\n'
        )

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", urlopen)
    gateway_chat._run_gateway_chat_streaming(
        session.session_id, "hello", "gpt-5", str(tmp_path), stream_id,
        model_provider="openai",
    )

    assert captured, "worker did not construct a Gateway request"
    url, authorization, endpoint, payload = captured[0]
    assert url.startswith("http://work-gateway:8650/")
    assert authorization == (f"Bearer {work_key}" if work_key else None)
    assert endpoint == ("http://work-gateway:8650", work_key)
    assert payload["reasoning_effort"] == expected
    if http_error:
        emitted = [events.get_nowait() for _ in range(events.qsize())]
        error = next(data for event, data, *_ in emitted if event == "apperror")
        if runs_api:
            assert error["type"] == "auth_mismatch"
        else:
            assert error["type"] == "gateway_auth_error"
            assert error["hint"].startswith("Check that" if work_key else "Set ")
    assert stream_id not in gateway_chat._STREAM_ENDPOINTS
