"""PID 1 supervisor: the web page remains available if voice startup fails."""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import os
import re
import signal
import sys
import time

from aiohttp import web

from .server import create_app
from .settings import SettingsStore


class Supervisor:
    def __init__(self, store, arguments):
        self.store, self.arguments = store, arguments
        self.lines = collections.deque(maxlen=300)
        self.process = None
        self.started = time.time()
        self.ready = False
        self.failures = 0
        self.applied_revision = store.revision()
        self.stopping = False

    def redact(self, line):
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        try:
            config = self.store.read()
            values = [config.get(k, "") for k in ("openai_key", "password", "cookie", "account")]
            values += [p.get("openai_key", "") for p in self.store.profiles()]
            for value in values:
                if isinstance(value, str) and len(value) >= 4:
                    line = line.replace(value, "[已隐藏]")
        except Exception:
            return "日志读取失败，敏感内容已隐藏"
        line = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", line)
        line = re.sub(r"(?i)(token|authorization|password|cookie|passToken)([\"'\s:=]+)[^,\s}]+", r"\1\2[已隐藏]", line)
        return line.rstrip()[:2000]

    def status(self):
        running = self.process is not None and self.process.returncode is None
        return {"running": running, "ready": running and self.ready, "worker_restarts": self.failures,
                "uptime": int(time.time() - self.started), "pid": self.process.pid if running else None,
                "web_bind": os.getenv("XIAOGPT_WEB_HOST", "127.0.0.1") + ":" + os.getenv("XIAOGPT_WEB_PORT", "9988"),
                "version": "1.1.1", "restart_mode": "container" if os.getpid() == 1 else "process"}

    async def run(self):
        try:
            while not self.stopping:
                self.ready = False
                self.applied_revision = self.store.revision()
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "-m", "xiaogpt.web.worker", *self.arguments,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                )
                async for raw in self.process.stdout:
                    line = self.redact(raw.decode(errors="replace"))
                    if "Running xiaogpt now" in line:
                        self.ready = True
                    self.lines.append(line)
                    print(line, flush=True)
                await self.process.wait()
                if not self.stopping:
                    self.failures += 1
                    self.lines.append("语音服务已退出，5 秒后自动重试；可在管理页检查设置。")
                    await asyncio.sleep(5)
        finally:
            await self.stop()

    async def stop(self):
        self.stopping = True
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()


async def serve():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default="/config/config.json")
    known, _ = parser.parse_known_args()
    if "--help" in sys.argv or "-h" in sys.argv:
        from xiaogpt.cli import main
        main()
        return
    arguments = sys.argv[1:]
    if not any(a == "--config" or a.startswith("--config=") for a in arguments):
        arguments += ["--config=" + known.config]
    store = SettingsStore(known.config)
    supervisor = Supervisor(store, arguments)
    event = asyncio.Event()
    app = create_app(store, supervisor, restart_event=event)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, event.set)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host, port = os.getenv("XIAOGPT_WEB_HOST", "127.0.0.1"), int(os.getenv("XIAOGPT_WEB_PORT", "9988"))
    try:
        await web.TCPSite(runner, host, port).start()
        print(f"XiaoGPT Web ready at http://{host}:{port}", flush=True)
        print("首次管理密码保存在 /config/web-initial-password.txt", flush=True)
        task = asyncio.create_task(supervisor.run())
        try:
            await event.wait()
        finally:
            supervisor.stopping = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    if os.getenv("XIAOGPT_WEB_DISABLED") == "1":
        os.execv(sys.executable, [sys.executable, "-u", "-m", "xiaogpt.web.worker", *sys.argv[1:]])
    asyncio.run(serve())
