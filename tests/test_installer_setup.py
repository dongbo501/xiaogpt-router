"""Validate installer input handling and credential-file protection offline."""

import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("router_setup", ROOT / "deploy/router/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = {
            "account": "test-account", "password": "quotes'\"\\$;中文",
            "openai_key": "test-key", "mi_did": "123",
        }

    def test_credentials_round_trip_and_permissions(self):
        setup.save_configuration(self.directory, self.config, "192.168.1.1")
        self.assertEqual(json.loads((self.directory / "config.json").read_text()), self.config)
        self.assertEqual((self.directory / "router.env").read_text(), "XIAOGPT_HOSTNAME=192.168.1.1\n")
        for name in ("config.json", "router.env"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)

    def test_existing_files_are_never_overwritten(self):
        setup.save_configuration(self.directory, self.config, "192.168.1.1")
        with self.assertRaises(RuntimeError):
            setup.save_configuration(self.directory, {"password": "replacement"}, "192.168.1.2")
        self.assertEqual(json.loads((self.directory / "config.json").read_text()), self.config)

    def test_invalid_address_cannot_inject_environment(self):
        for address in ("0.0.0.0", "127.0.0.1", "224.0.0.1", "::1", "192.168.1.1\nOPENAI_API_KEY=x"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                setup.validate_address(address)

    def test_api_url_requires_a_plain_http_endpoint(self):
        self.assertEqual(setup.validate_api_base("https://example.com/v1/"), "https://example.com/v1")
        for url in ("example.com", "file:///etc/passwd", "https://user:pass@example.com", "https://example.com?key=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                setup.validate_api_base(url)

    def test_selects_device_and_retries_invalid_number(self):
        devices = [
            {"name": "A", "miotDID": "123", "hardware": "L05C"},
            {"name": "B", "miotDID": "456", "hardware": "LX06"},
            {"name": "Incomplete device"},
        ]
        with patch("builtins.input", side_effect=["0", "bad", "2"]):
            self.assertEqual(setup.choose_device(devices), devices[1])

    async def test_resume_preserves_credentials_and_checks_selected_device(self):
        setup.save_configuration(self.directory, self.config, "192.168.1.1")
        before = (self.directory / "config.json").read_bytes()
        with (
            patch.object(setup, "check_local_address") as check_address,
            patch.object(setup, "login_devices", new_callable=AsyncMock) as login,
            patch("builtins.input", side_effect=AssertionError("Must not prompt again")),
        ):
            login.return_value = [{"miotDID": "123"}]
            await setup.configure(self.directory)
            check_address.assert_called_once_with("192.168.1.1")
            login.return_value = [{"miotDID": "456"}]
            with self.assertRaises(RuntimeError):
                await setup.configure(self.directory)
        self.assertEqual((self.directory / "config.json").read_bytes(), before)

    async def test_incomplete_existing_configuration_is_preserved(self):
        target = self.directory / "config.json"
        target.write_text("keep me")
        with self.assertRaises(RuntimeError):
            await setup.configure(self.directory)
        self.assertEqual(target.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
