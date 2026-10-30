"""Authenticated local management API, with no Docker socket access."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

import httpx
from aiohttp import web

from .settings import ConflictError, SettingsStore, ValidationError, write_json

STATIC = Path(__file__).parent / "static"
COOKIE = "xiaogpt_admin"


class Auth:
    def __init__(self, directory):
        self.path = Path(directory) / "web-admin.json"
        self.initial_path = Path(directory) / "web-initial-password.txt"
        if not self.path.exists():
            password = secrets.token_urlsafe(12)
            self.set_password(password)
            from .settings import atomic_write
            atomic_write(self.initial_path, password + "\n")
        self.data = json.loads(self.path.read_text())
        self.attempts = collections.defaultdict(collections.deque)

    def set_password(self, password):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValidationError("管理密码须为 10–128 个字符")
        salt = secrets.token_hex(16)
        self.data = {
            "salt": salt,
            "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000).hex(),
            "signing": secrets.token_hex(32),
        }
        write_json(self.path, self.data)

    def verify(self, password):
        if not isinstance(password, str) or len(password) > 128:
            return False
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self.data["salt"]), 260000).hex()
        return hmac.compare_digest(value, self.data["hash"])

    def sign(self, value):
        return hmac.new(bytes.fromhex(self.data["signing"]), value.encode(), hashlib.sha256).hexdigest()

    def issue(self):
        value = secrets.token_hex(16) + "." + str(int(time.time()) + 43200)
        return value + "." + self.sign(value)

    def valid(self, token):
        try:
            value, signature = token.rsplit(".", 1)
            return int(value.rsplit(".", 1)[1]) > time.time() and hmac.compare_digest(signature, self.sign(value))
        except (AttributeError, ValueError):
            return False

    def csrf(self, token):
        return self.sign("csrf:" + token)

    def limited(self, address):
        attempts = self.attempts[address]
        while attempts and attempts[0] < time.monotonic() - 60:
            attempts.popleft()
        return len(attempts) >= 5


def public_error(error):
    text = str(error).lower()
    status = getattr(error, "status_code", None)
    if status in (401, 403) or "invalid_api_key" in text:
        return "接口拒绝了密钥，请检查地址和 API Key 是否属于同一服务商。"
    if status == 429 or "rate limit" in text or "tokens per minute" in text:
        return "接口服务暂时限流，请稍后再试或检查服务商额度。"
    if "insufficient_quota" in text:
        return "接口余额或额度不足，请到服务商后台检查。"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "接口响应超时，请检查地址和网络后重试。"
    if status == 404:
        return "接口路径或模型不存在，请检查 Base URL 和模型名称。"
    return "接口未返回有效回答，请检查服务商状态、模型和接口格式。"


def create_app(store: SettingsStore, supervisor, *, restart_event=None):
    auth = Auth(store.directory)
    lock = asyncio.Lock()
    probe_lock = asyncio.Lock()
    event = restart_event or asyncio.Event()

    @web.middleware
    async def middleware(request, handler):
        try:
            if request.path.startswith("/api/") and request.path not in ("/api/login", "/api/session"):
                token = request.cookies.get(COOKIE, "")
                if not auth.valid(token):
                    raise web.HTTPUnauthorized(text='{"error":"请先登录管理页面"}', content_type="application/json")
                if request.method not in ("GET", "HEAD") and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), auth.csrf(token)):
                    raise web.HTTPForbidden(text='{"error":"页面验证已过期，请刷新后重试"}', content_type="application/json")
            origin = request.headers.get("Origin")
            if request.method not in ("GET", "HEAD") and origin and origin not in (f"http://{request.host}", f"https://{request.host}"):
                raise web.HTTPForbidden(text='{"error":"不允许跨站请求"}', content_type="application/json")
            response = await handler(request)
        except ConflictError as error:
            response = web.json_response({"error": str(error)}, status=409)
        except ValidationError as error:
            response = web.json_response({"error": str(error)}, status=400)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = web.json_response({"error": "请求内容格式不正确"}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            response = web.json_response({"error": "操作失败，配置文件未确认保存，请刷新后检查"}, status=500)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    app = web.Application(middlewares=[middleware], client_max_size=65536)
    app["auth"] = auth
    app["restart_event"] = event
    app["store"] = store

    async def body(request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValidationError("请求必须为对象")
        return value

    async def session(request):
        token = request.cookies.get(COOKIE, "")
        valid = auth.valid(token)
        return web.json_response({"authenticated": valid, "csrf": auth.csrf(token) if valid else "", "initial_password": auth.initial_path.exists()})

    def login_response():
        token = auth.issue()
        response = web.json_response({"authenticated": True, "csrf": auth.csrf(token)})
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=43200, path="/")
        return response

    async def login(request):
        address = request.remote or "unknown"
        if auth.limited(address):
            return web.json_response({"error": "尝试次数过多，请一分钟后再试"}, status=429)
        payload = await body(request)
        if not await asyncio.to_thread(auth.verify, payload.get("password", "")):
            auth.attempts[address].append(time.monotonic())
            return web.json_response({"error": "管理密码不正确"}, status=401)
        auth.attempts[address].clear()
        return login_response()

    async def logout(request):
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE)
        return response

    async def password(request):
        payload = await body(request)
        if not await asyncio.to_thread(auth.verify, payload.get("current", "")):
            raise ValidationError("当前管理密码不正确")
        async with lock:
            await asyncio.to_thread(auth.set_password, payload.get("password", ""))
            auth.initial_path.unlink(missing_ok=True)
        return login_response()

    async def settings(request):
        if request.method == "GET":
            return web.json_response(store.public())
        payload = await body(request)
        async with lock:
            result = store.save(payload)
        return web.json_response(result)

    async def profiles(request):
        payload = await body(request)
        async with lock:
            if request.method == "DELETE":
                store.delete_profile(payload.get("id", ""))
                return web.json_response({"ok": True})
            identifier = store.save_profile(payload)
        return web.json_response({"id": identifier})

    async def status(request):
        return web.json_response({**supervisor.status(), "pending_changes": store.revision() != supervisor.applied_revision, "restarting": event.is_set()})

    async def logs(request):
        return web.json_response({"lines": list(supervisor.lines)[-120:]})

    async def restart(request):
        # A response must reach the browser before PID 1 exits. Docker's
        # unless-stopped policy then restarts this container, without a socket.
        if not event.is_set():
            asyncio.get_running_loop().call_later(0.75, event.set)
        return web.json_response({"ok": True, "message": "容器正在重启，页面会自动重新连接"})

    async def backups(request):
        return web.json_response({"backups": store.backups()})

    async def restore(request):
        payload = await body(request)
        async with lock:
            store.restore(payload.get("id", ""), payload.get("revision"))
        return web.json_response(store.public())

    async def probe(request):
        payload = await body(request)
        if request.path.endswith("models") and not payload.get("model"):
            payload["model"] = "list-models"
        connection = store.connection(payload)
        if probe_lock.locked():
            return web.json_response({"error": "已有接口测试正在进行，请稍候"}, status=409)
        started = time.monotonic()
        try:
            async with probe_lock, asyncio.timeout(45):
                import openai
                async with httpx.AsyncClient(trust_env=True, proxy=store.read().get("proxy"), timeout=35) as http:
                    async with openai.AsyncOpenAI(
                        api_key=connection["openai_key"], base_url=connection["api_base"],
                        http_client=http, max_retries=0,
                    ) as client:
                        if request.path.endswith("models"):
                            models = await client.models.list()
                            return web.json_response({"models": sorted({m.id for m in models.data})[:500]})
                        stream = await client.chat.completions.create(
                            model=connection["model"], stream=True,
                            messages=[{"role": "user", "content": "请只回复：接口测试成功。"}],
                        )
                        text = ""
                        async with stream:
                            async for part in stream:
                                if part.choices:
                                    text += part.choices[0].delta.content or ""
                                if len(text) >= 500:
                                    break
                        if not text.strip():
                            raise ValidationError("接口接受了请求，但没有返回文字，请确认模型支持聊天流式接口")
                        return web.json_response({"ok": True, "text": text[:500], "seconds": round(time.monotonic() - started, 2)})
        except ValidationError:
            raise
        except Exception as error:
            return web.json_response({"error": public_error(error)}, status=502)

    async def devices(request):
        config = Config(**store.read())
        try:
            from aiohttp import ClientSession, ClientTimeout
            from miservice import MiAccount, MiNAService
            async with asyncio.timeout(20), ClientSession(timeout=ClientTimeout(total=18)) as session:
                account = MiAccount(session, config.account, config.password, str(store.directory / ".mi.token"))
                result = await MiNAService(account).device_list()
            return web.json_response({"devices": [{"name": d.get("name", "小爱音箱"), "mi_did": str(d["miotDID"]), "hardware": d["hardware"]} for d in result if d.get("miotDID") and d.get("hardware")]})
        except Exception:
            return web.json_response({"error": "读取音箱列表失败，请检查小米登录状态；也可以手动填写 DID 和型号"}, status=502)

    async def static(request):
        name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[request.path]
        return web.FileResponse(STATIC / name)

    app.router.add_get("/api/session", session)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", password)
    app.router.add_get("/api/settings", settings)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/profiles", profiles)
    app.router.add_delete("/api/profiles", profiles)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/logs", logs)
    app.router.add_post("/api/restart", restart)
    app.router.add_get("/api/backups", backups)
    app.router.add_post("/api/restore", restore)
    app.router.add_post("/api/test", probe)
    app.router.add_post("/api/models", probe)
    app.router.add_post("/api/devices", devices)
    for path in ("/", "/app.js", "/style.css"):
        app.router.add_get(path, static)
    return app
