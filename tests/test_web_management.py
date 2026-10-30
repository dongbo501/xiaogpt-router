"""Management configuration, authentication, and lifecycle regression tests."""
import asyncio
import collections
import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from xiaogpt.config import Config
from xiaogpt.web.settings import SettingsStore, ValidationError, ConflictError, api_base
from xiaogpt.web.server import Auth, create_app, public_error
from xiaogpt.xiaogpt import MiGPT


def fixture():
    return {
        "account": "private-account", "password": "private-mi-password",
        "api_base": "https://first.example/v1", "openai_key": "private-api-key",
        "gpt_options": {"model": "test-model"}, "prompt": "existing prompt",
        "hardware": "L05C", "mi_did": "123", "keyword": ["请问"],
        "stream": True, "tts": "edge", "tts_options": {"voice": "zh-CN-XiaoxiaoNeural", "volume": "+40%"},
        "mute_xiaoai": True, "use_command": True,
        "start_conversation": "开始持续对话", "end_conversation": "结束持续对话",
    }


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps(fixture()))
        self.store = SettingsStore(self.path)

    def payload(self, **values):
        return {"revision": self.store.revision(), "config": values}

    def test_public_configuration_and_profiles_never_include_credentials(self):
        value = json.dumps(self.store.public())
        for secret in ("private-account", "private-mi-password", "private-api-key"):
            self.assertNotIn(secret, value)
        self.assertTrue(self.store.public()["key_saved"])

    def test_base_url_normalization_and_credential_rejection(self):
        self.assertEqual(api_base("https://example.com"), "https://example.com/v1")
        self.assertEqual(api_base("https://example.com/v1/chat/completions"), "https://example.com/v1")
        self.assertEqual(api_base("http://127.0.0.1:8000/custom/"), "http://127.0.0.1:8000/custom")
        for value in ("ftp://example.com", "https://user:pass@example.com", "https://example.com?key=secret", "https://example.com/#x", "https://example.com:invalid"):
            with self.subTest(value=value), self.assertRaises(ValidationError): api_base(value)

    def test_url_change_requires_matching_key(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValidationError, "对应的 API Key"):
            self.store.save(self.payload(api_base="https://second.example"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_switch_profile_uses_its_own_key(self):
        identifier = self.store.save_profile({"name": "备用", "api_base": "https://second.example", "model": "second", "openai_key": "second-secret"})
        payload = self.payload(api_base="https://second.example/v1", gpt_options={"model": "second"})
        payload["profile_id"] = identifier
        self.store.save(payload)
        current = self.store.read()
        self.assertEqual(current["openai_key"], "second-secret")
        self.assertEqual(current["password"], "private-mi-password")
        self.assertEqual(current["api_base"], "https://second.example/v1")

    def test_save_preserves_secrets_and_backups_are_private(self):
        self.store.save(self.payload(waiting_prompt="我来想想。", error_prompt=""))
        self.assertEqual(self.store.read()["openai_key"], "private-api-key")
        self.assertEqual(self.store.read()["waiting_prompt"], "我来想想。")
        backup = self.store.backup_dir / self.store.backups()[0]["id"]
        for path in (self.path, backup, self.store.profiles_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_proxy_credentials_are_hidden_and_preserved(self):
        config = self.store.read()
        config["tts_options"]["proxy"] = "http://proxy-user:proxy-secret@example.com:8080"
        self.path.write_text(json.dumps(config))
        self.assertNotIn("proxy-secret", json.dumps(self.store.public()))
        self.store.save(self.payload(tts_options={"voice": "zh-CN-XiaoxiaoNeural"}))
        self.assertEqual(self.store.read()["tts_options"]["proxy"], config["tts_options"]["proxy"])

    def test_worker_logs_redact_credentials(self):
        from xiaogpt.web.__main__ import Supervisor
        supervisor = Supervisor(self.store, [])
        redacted = supervisor.redact("private-api-key private-mi-password private-account sk-another-secret")
        for secret in ("private-api-key", "private-mi-password", "private-account", "sk-another-secret"):
            self.assertNotIn(secret, redacted)

    def test_external_change_is_not_overwritten(self):
        body = self.payload(prompt="web edit")
        current = self.store.read(); current["prompt"] = "external edit"
        self.path.write_text(json.dumps(current))
        with self.assertRaises(ConflictError): self.store.save(body)
        self.assertEqual(self.store.read()["prompt"], "external edit")

    def test_restore_is_revision_checked_and_path_restricted(self):
        self.store.save(self.payload(prompt="changed"))
        identifier = self.store.backups()[0]["id"]
        with self.assertRaises(ValidationError): self.store.restore("../config.json", self.store.revision())
        with self.assertRaises(ConflictError): self.store.restore(identifier, "stale")
        self.store.restore(identifier, self.store.revision())
        self.assertEqual(self.store.read()["prompt"], "existing prompt")

    def test_invalid_settings_never_write(self):
        for values in (
            {"keyword": []}, {"stream": "yes"}, {"tts_options": {"volume": "+999%"}},
            {"gpt_options": {"model": "test", "max_tokens": 1.5}},
            {"prompt": "x" * 16001}, {"start_conversation": "结束持续对话"},
            {"password": "overwritten"}, {"tts": "unsupported"},
        ):
            with self.subTest(values=list(values)), self.assertRaises(ValidationError):
                self.store.save(self.payload(**values))
        self.assertEqual(self.store.read(), fixture())

    def test_yaml_format_is_preserved(self):
        import yaml
        path = self.path.with_suffix(".yaml")
        path.write_text(yaml.safe_dump(fixture()))
        store = SettingsStore(path)
        store.save({"revision": store.revision(), "config": {"prompt": "中文新提示"}})
        self.assertEqual(yaml.safe_load(path.read_text())["prompt"], "中文新提示")


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps(fixture()))
        self.store = SettingsStore(self.path)
        self.supervisor = SimpleNamespace(
            lines=collections.deque(["test log"]), applied_revision=self.store.revision(),
            status=lambda: {"running": True, "ready": True, "uptime": 20},
        )
        self.app = create_app(self.store, self.supervisor)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.password = self.app["auth"].initial_path.read_text().strip()
        self.csrf = ""

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post("/api/login", json={"password": self.password})
        self.assertEqual(response.status, 200)
        self.csrf = (await response.json())["csrf"]

    async def post(self, path, body):
        return await self.client.post(path, json=body, headers={"X-CSRF-Token": self.csrf})

    async def test_anonymous_requests_cannot_read_settings_logs_or_mutate(self):
        for path in ("/api/settings", "/api/logs", "/api/status", "/api/backups"):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.post("/api/restart", json={})).status, 401)
        self.assertFalse(self.app["restart_event"].is_set())

    async def test_session_csrf_and_cross_origin_protection(self):
        await self.login()
        self.assertEqual((await self.client.post("/api/restart", json={})).status, 403)
        response = await self.client.post("/api/restart", json={}, headers={"X-CSRF-Token": self.csrf, "Origin": "https://evil.invalid"})
        self.assertEqual(response.status, 403)
        self.assertFalse(self.app["restart_event"].is_set())
        self.assertTrue((await (await self.client.get("/api/session")).json())["authenticated"])

    async def test_saved_key_never_appears_in_settings(self):
        await self.login()
        response = await self.client.get("/api/settings")
        self.assertEqual(response.status, 200)
        self.assertNotIn("private-api-key", await response.text())

    async def test_restart_responds_before_shutdown_event(self):
        await self.login()
        response = await self.post("/api/restart", {})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.app["restart_event"].is_set())
        await asyncio.wait_for(self.app["restart_event"].wait(), 2)

    async def test_password_change_persists_and_invalidates_old_sessions(self):
        await self.login()
        old_token = self.app["auth"].issue()
        response = await self.post("/api/password", {"current": self.password, "password": "new-private-password"})
        self.assertEqual(response.status, 200)
        reloaded = Auth(self.path.parent)
        self.assertTrue(reloaded.verify("new-private-password"))
        self.assertFalse(reloaded.valid(old_token))
        self.assertFalse(reloaded.initial_path.exists())

    async def test_login_attempts_are_limited(self):
        for _ in range(5):
            self.assertEqual((await self.client.post("/api/login", json={"password": "wrong"})).status, 401)
        self.assertEqual((await self.client.post("/api/login", json={"password": self.password})).status, 429)

    async def test_saved_settings_require_restart_and_support_conflict_detection(self):
        await self.login()
        payload = {"revision": self.store.revision(), "config": {"prompt": "new prompt"}}
        self.assertEqual((await self.post("/api/settings", payload)).status, 200)
        state = await (await self.client.get("/api/status")).json()
        self.assertTrue(state["pending_changes"])
        self.assertEqual((await self.post("/api/settings", payload)).status, 409)

    async def test_static_assets_have_security_headers_and_no_inline_script(self):
        for path in ("/", "/app.js", "/style.css"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        html = await (await self.client.get("/")).text()
        self.assertNotIn("onclick=", html)

    async def test_sessions_survive_service_restart(self):
        await self.login()
        token = self.app["auth"].issue()
        self.assertTrue(Auth(self.path.parent).valid(token))


class PromptConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_edge_prompt_and_disabled_prompt(self):
        bot = MiGPT(Config(**fixture(), waiting_prompt="我来帮你查一查"))
        bot.tts = SimpleNamespace(cache_prompt=AsyncMock(return_value="cached.mp3"), play_file=AsyncMock())
        try:
            await bot._play_waiting_prompt()
            self.assertEqual(bot.tts.cache_prompt.call_args.args[0], "我来帮你查一查")
            bot.config.waiting_prompt = ""
            await bot._play_waiting_prompt()
            self.assertEqual(bot.tts.play_file.await_count, 1)
            bot.config.rate_limit_prompt = "请求太多啦"
            await bot._play_answer_error(RuntimeError("Rate limit reached"))
            self.assertEqual(bot.tts.cache_prompt.call_args.args[0], "请求太多啦")
            bot.config.error_prompt = ""
            await bot._play_answer_error(RuntimeError("offline"))
            self.assertEqual(bot.tts.play_file.await_count, 2)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
