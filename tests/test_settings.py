"""Connections, keys and models: config.toml edits, the secrets file, and the settings API."""

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeModel, reply
from test_api import Server

from nano_workbuddy import settings
from nano_workbuddy.config import SecretStore, key_status, load, secret
from nano_workbuddy.runtime import Workspace

KEY = "sk-test-0123456789abcdef"


def config_text(home):
    return (home / "config.toml").read_text()


def test_secret_store_is_private_and_atomic(home):
    store = SecretStore(home)
    store.set("a", "one")
    store.set("b", "two")
    assert stat.S_IMODE((home / "secrets.json").stat().st_mode) == 0o600
    assert store.get("a") == "one"
    store.delete("a")
    assert store.get("a") is None and store.get("b") == "two"
    assert not [p for p in home.iterdir() if p.name.startswith(".secrets-")]  # no temp files left behind


async def test_tavily_key_uses_model_secret_store_without_echo_or_network(home, monkeypatch):
    load(home)
    settings.put_provider(home, "work", "openai-chat", "https://example.test/v1", api_key=KEY)
    ws = Workspace(load(home))
    monkeypatch.setattr(ws, "search_provider", lambda: pytest.fail("saving must not request Tavily"))
    async with Server(ws) as server:
        key = "tvly-test-private-9876543210"
        response = await server.client.put("/api/settings/search", json={"api_key": key})
        assert response.status_code == 200 and key not in response.text
        assert response.json()["search"]["key"]["hint"] == "…3210"
        cfg = load(home)
        assert cfg.search.api_key == "secret:search" and key not in config_text(home)
        assert secret(cfg.search.api_key, home) == key
        assert secret(cfg.providers["work"].api_key, home) == KEY
        assert stat.S_IMODE((home / "secrets.json").stat().st_mode) == 0o600
        assert key not in (await server.client.get("/api/settings")).text
        replacement = "tvly-replacement-abcdef"
        response = await server.client.put("/api/settings/search", json={"api_key": replacement})
        assert replacement not in response.text
        assert secret(load(home).search.api_key, home) == replacement
        await server.client.put("/api/settings/search", json={"clear": True})
        assert not load(home).search.api_key
        assert SecretStore(home).get("search") is None
        assert secret(load(home).providers["work"].api_key, home) == KEY


def test_put_provider_stores_key_outside_config_and_keeps_comments(home):
    load(home)  # writes the default config
    (home / "config.toml").write_text("# my notes\n" + config_text(home))
    cfg = settings.put_provider(home, "od-work", "openai-chat", "https://example.com/v1/", api_key=KEY)
    text = config_text(home)
    assert "# my notes" in text and KEY not in text
    assert cfg.providers["od-work"].api_key == "secret:od-work"
    assert cfg.providers["od-work"].base_url == "https://example.com/v1"
    assert secret("secret:od-work", home) == KEY
    assert (home / "config.toml.bak").exists()
    status = key_status("secret:od-work", home)
    assert status == {"source": "secret", "name": "od-work", "set": True, "hint": "…cdef"}

    settings.put_provider(home, "od-work", "openai-chat", "https://example.com/v1")  # no key: keep the old one
    assert secret("secret:od-work", home) == KEY
    cfg = settings.put_provider(home, "od-work", "openai-chat", "https://example.com/v1", key_ref="env:MY_KEY")
    assert cfg.providers["od-work"].api_key == "env:MY_KEY"


@pytest.mark.parametrize(("name", "protocol", "url"), [
    ("bad name", "openai-chat", "https://x"), ("ok", "grpc", "https://x"), ("ok", "openai-chat", "ftp://x")])
def test_invalid_changes_write_nothing(home, name, protocol, url):
    load(home)
    before = config_text(home)
    with pytest.raises(settings.SettingsError):
        settings.put_provider(home, name, protocol, url, api_key=KEY)
    assert config_text(home) == before and not (home / "secrets.json").exists()


def test_models_defaults_and_deletion_rules(home):
    cfg = load(home)
    settings.put_provider(home, "alt", "anthropic", "https://api.example.com", api_key=KEY)
    cfg = settings.put_model(home, "claude", "alt", "claude-opus-5-5", 1_000_000, 64_000,
                             {"thinking": {"type": "adaptive"}})
    assert cfg.models["claude"].params == {"thinking": {"type": "adaptive"}}
    with pytest.raises(settings.SettingsError, match="unknown provider"):
        settings.put_model(home, "x", "nope", "id", 100_000, 1_000)
    with pytest.raises(settings.SettingsError, match="larger than max output"):
        settings.put_model(home, "x", "alt", "id", 10_000, 20_000)
    with pytest.raises(settings.SettingsError, match="used by models"):
        settings.delete_provider(home, "alt", cfg)
    cfg = settings.set_default(home, "claude")
    with pytest.raises(settings.SettingsError, match="default model"):
        settings.delete_model(home, "claude")
    cfg = settings.set_default(home, "deepseek-flash")
    cfg = settings.delete_model(home, "claude")
    cfg = settings.delete_provider(home, "alt", cfg)
    assert "alt" not in cfg.providers and SecretStore(home).get("alt") is None


async def test_session_on_a_deleted_model_falls_back_to_the_default(home, project):
    model = FakeModel(reply("ok"))
    ws = Workspace(load(home), connect=model.connect)
    ws.cfg = settings.put_model(home, "extra", "deepseek", "deepseek-extra", 100_000, 8_000)
    log = ws.new_session(project, "extra")
    ws.cfg = settings.delete_model(home, "extra")
    assert await ws.submit(log, "hi") == "ok"
    assert model.spec.name == "deepseek-flash"


@pytest.fixture
def fake_openai():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _auth_ok(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                error = {"error": {"message": "invalid api key", "code": "invalid_api_key"}}
                self.wfile.write(json.dumps(error).encode())
                return False
            return True

        def do_GET(self):
            if self._auth_ok():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"object": "list", "data": [
                    {"id": "zeta-model", "object": "model", "created": 0, "owned_by": "t"},
                    {"id": "alpha-model", "object": "model", "created": 0, "owned_by": "t"}]}).encode())

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self._auth_ok():
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m"}
                for chunk in ({**base, "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]},
                              {**base, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", seen
    srv.shutdown()


async def test_settings_api_end_to_end(home, fake_openai):
    url, seen = fake_openai
    async with Server(Workspace(load(home))) as s:  # the real connect: keys come from secrets.json
        c = s.client
        r = await c.put("/api/settings/providers/local", json={"protocol": "openai-chat", "base_url": url,
                                                                "api_key": KEY})
        assert r.status_code == 200 and KEY not in r.text
        listed = (await c.get("/api/settings/providers/local/models")).json()
        assert [m["id"] for m in listed] == ["alpha-model", "zeta-model"]

        r = await c.put("/api/settings/models/alpha", json={"provider": "local", "id": "alpha-model",
                                                             "context_window": 128_000, "max_output": 4_000})
        assert r.status_code == 200
        assert (await c.post("/api/settings/models/alpha/test")).json()["reply"] == "OK"
        assert all(h == f"Bearer {KEY}" for h in seen)

        await c.put("/api/settings/default-model", json={"name": "alpha"})
        info = (await c.get("/api/info")).json()
        assert info["default_model"] == "alpha" and {"name": "alpha", "provider": "local",
                                                     "context_window": 128_000} in info["models"]
        view = (await c.get("/api/settings")).json()
        assert KEY not in json.dumps(view)
        assert next(p for p in view["providers"] if p["name"] == "local")["key"]["hint"] == "…cdef"

        r = await c.put("/api/settings/providers/local", json={"protocol": "openai-chat", "base_url": url,
                                                                "api_key": "sk-wrong-key-000000"})
        result = (await c.post("/api/settings/models/alpha/test")).json()
        assert not result["ok"] and "401" in result["error"]
        assert (await c.get("/api/settings/providers/local/models")).status_code == 502
        assert (await c.delete("/api/settings/providers/local")).status_code == 400  # still used by "alpha"
        bad = await c.put("/api/settings/providers/x", json={"protocol": "openai-chat", "base_url": url,
                                                             "key_ref": "plain-text-key"})
        assert bad.status_code == 400
