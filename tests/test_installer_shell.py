"""Run the real installer in a PTY with offline Docker/download stand-ins."""

import io
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_TOOL = r'''
import json, os, pathlib, shutil, sys
tool, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as out:
    out.write(json.dumps([tool] + args) + '\n')
if tool == 'uname':
    print('Linux' if args == ['-s'] else 'x86_64')
elif tool == 'curl':
    if os.environ.get('FAIL_DOWNLOAD'): sys.exit(22)
    shutil.copyfile(os.environ['SOURCE_TAR'], args[args.index('-o') + 1])
elif tool == 'docker':
    if args[:2] == ['container', 'inspect']:
        sys.exit(0 if os.environ.get('EXISTING_CONTAINER') else 1)
    if args[0] == 'build' and os.environ.get('FAIL_BUILD'): sys.exit(1)
    if args[0] == 'pull' and os.environ.get('FAIL_PULL'): sys.exit(1)
    if args[0] == 'run' and '--rm' in args:
        if os.environ.get('FAIL_SETUP'): sys.exit(1)
        for index, value in enumerate(args):
            if value == '-v' and args[index + 1].endswith(':/config'):
                config = pathlib.Path(args[index + 1][:-len(':/config')])
                (config / 'config.json').write_text('{"account":"test"}')
                (config / 'router.env').write_text('XIAOGPT_HOSTNAME=192.168.1.1\n')
    if args[0] == 'inspect': print('exited 1' if os.environ.get('FAIL_START') else 'running 0')
    if args[0] == 'logs': print('Running xiaogpt now')
'''


class InstallerShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.destination = self.base / "persistent disk" / "xiaogpt"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "calls.jsonl"
        for name in ("uname", "curl", "docker"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            script.chmod(0o755)
        self.archive = self.base / "source.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name in ("deploy/router/Dockerfile", "deploy/router/setup.py"):
                content = b"fixture\n"
                info = tarfile.TarInfo("xiaogpt-router-main/" + name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        CALL_LOG=str(self.log), SOURCE_TAR=str(self.archive))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_installer(self, installer_args=(), **extra):
        master, slave = pty.openpty()
        output = bytearray()

        def drain():
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            result = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--dir", str(self.destination), *installer_args],
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(self.env, **extra), timeout=15,
            )
            os.close(slave)
            slave = None
            reader.join(timeout=2)
            return result.returncode, output.decode()
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
            reader.join(timeout=2)

    def test_fresh_install_pulls_latest_configures_and_starts_in_order(self):
        code, output = self.run_installer()
        self.assertEqual(code, 0, output)
        self.assertIn("安装完成", output)
        calls = self.calls()
        docker = [args for tool, *args in calls if tool == "docker"]
        self.assertLess(next(i for i, args in enumerate(docker) if args[0] == "pull"),
                        next(i for i, args in enumerate(docker) if args[:2] == ["run", "--rm"]))
        launch = next(args for args in docker if args[:2] == ["run", "-d"])
        self.assertIn("ghcr.io/dongbo501/xiaogpt-router:latest", launch)
        self.assertFalse(any(args[0] == "build" for args in docker))
        self.assertIn(str(self.destination / "config") + ":/config", launch)
        self.assertIn(str(self.destination / "config/router.env"), launch)
        self.assertIn("--config=/config/config.json", launch)
        self.assertEqual(launch[launch.index("--restart") + 1], "unless-stopped")
        self.assertFalse((self.destination / ".install-lock").exists())
        self.assertFalse((self.destination / ".source.tar.gz").exists())

    def test_existing_container_stops_before_download_or_directory_creation(self):
        code, output = self.run_installer(EXISTING_CONTAINER="1")
        self.assertNotEqual(code, 0)
        self.assertIn("已有名为 xiaogpt", output)
        self.assertFalse(self.destination.exists())
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_existing_unmanaged_directory_is_untouched(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination / "important.txt"
        sentinel.write_text("keep")
        code, _ = self.run_installer()
        self.assertNotEqual(code, 0)
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "keep")

    def test_download_failure_does_not_build_or_start(self):
        code, _ = self.run_installer(FAIL_DOWNLOAD="1")
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call[:2] in (["docker", "build"], ["docker", "run"]) for call in self.calls()))
        self.assertFalse((self.destination / ".install-lock").exists())

    def test_build_failure_does_not_configure_or_start(self):
        code, _ = self.run_installer(installer_args=("--build",), FAIL_BUILD="1")
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.calls()))

    def test_pull_failure_does_not_configure_or_start(self):
        code, _ = self.run_installer(FAIL_PULL="1")
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.calls()))

    def test_explicit_source_build_remains_available(self):
        code, output = self.run_installer(installer_args=("--build",))
        self.assertEqual(code, 0, output)
        self.assertTrue(any(call[:2] == ["docker", "build"] for call in self.calls()))
        self.assertFalse(any(call[:2] == ["docker", "pull"] for call in self.calls()))
        launch = next(call for call in self.calls() if call[:3] == ["docker", "run", "-d"])
        self.assertIn("xiaogpt-router:local", launch)

    def test_setup_failure_can_resume_without_redownloading(self):
        code, _ = self.run_installer(FAIL_SETUP="1")
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call[:3] == ["docker", "run", "-d"] for call in self.calls()))
        code, output = self.run_installer()
        self.assertEqual(code, 0, output)
        self.assertEqual(sum(call[0] == "curl" for call in self.calls()), 1)

    def test_failed_container_is_not_reported_as_success(self):
        code, output = self.run_installer(FAIL_START="1")
        self.assertNotEqual(code, 0)
        self.assertNotIn("安装完成", output)
        self.assertIn("容器未正常运行", output)
        self.assertTrue((self.destination / "config/config.json").exists())


if __name__ == "__main__":
    unittest.main()
