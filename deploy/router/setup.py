"""Interactive first-install configuration, run inside the router image."""

import asyncio
import getpass
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit


def ask(label, default="", secret=False):
    while True:
        prompt = f"{label}" + (f" [{default}]" if default else "") + "："
        value = (getpass.getpass(prompt) if secret else input(prompt))
        if not secret:
            value = value.strip()
        value = value or default
        if value:
            return value
        print("此项不能为空，请重新输入。")


def validate_address(value):
    address = ipaddress.IPv4Address(value)
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("请填写音箱能访问的路由器局域网 IPv4 地址")
    return str(address)


def check_local_address(hostname):
    # The installer uses host networking, so this verifies the router owns it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((hostname, 0))


def validate_api_base(value):
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("请填写完整的 http:// 或 https:// API Base URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Base URL 中不能包含密码、查询参数或片段")
    return value.rstrip("/")


def choose_device(devices):
    devices = [d for d in devices if d.get("miotDID") and d.get("hardware")]
    if not devices:
        raise RuntimeError("账号下没有可用的小爱音箱，请确认账号及设备绑定")
    print("\n请选择要接入的音箱：")
    for index, device in enumerate(devices, 1):
        print(f"  {index}. {device.get('name', '小爱音箱')} / {device['hardware']} / DID {device['miotDID']}")
    while True:
        selected = ask("音箱编号", "1" if len(devices) == 1 else "")
        if selected.isdigit() and 1 <= int(selected) <= len(devices):
            return devices[int(selected) - 1]
        print("请输入列表中的编号。")


def save_configuration(directory, config, hostname):
    # Exclusive creation avoids silently replacing credentials on a retry.
    config_file = directory / "config.json"
    env_file = directory / "router.env"
    if config_file.exists() or env_file.exists():
        raise RuntimeError("已有配置文件，拒绝覆盖")
    written = []
    try:
        for target, content in (
            (config_file, json.dumps(config, ensure_ascii=False, indent=2) + "\n"),
            (env_file, f"XIAOGPT_HOSTNAME={validate_address(hostname)}\n"),
        ):
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            written.append(target)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
    except BaseException:
        for target in written:
            target.unlink(missing_ok=True)
        raise


async def login_devices(config, directory):
    from aiohttp import ClientSession
    from miservice import MiAccount, MiNAService

    print("\n正在登录小米账号；如需短信或邮件验证码，请按终端提示输入。")
    # Library authentication errors can contain tokens; do not print them here.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with ClientSession() as session:
            account = MiAccount(
                session, config["account"], config["password"],
                str(directory / ".mi.token"),
            )
            return await MiNAService(account).device_list()
    except Exception:
        raise RuntimeError("小米登录或读取设备失败，请检查账号密码、验证码和网络后重试") from None
    finally:
        logging.disable(previous_logging)


async def configure(directory=Path("/config"), template=Path("/setup/config.example.yaml")):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    config_file, env_file = directory / "config.json", directory / "router.env"
    if config_file.exists() or env_file.exists():
        if not (config_file.exists() and env_file.exists()):
            raise RuntimeError("配置文件不完整，请检查 config.json 和 router.env；安装器不会覆盖它们")
        config = json.loads(config_file.read_text(encoding="utf-8"))
        env = env_file.read_text(encoding="utf-8").strip()
        if not env.startswith("XIAOGPT_HOSTNAME="):
            raise RuntimeError("router.env 缺少 XIAOGPT_HOSTNAME")
        hostname = validate_address(env.split("=", 1)[1])
        check_local_address(hostname)
        devices = await login_devices(config, directory)
        if not any(str(d.get("miotDID")) == str(config.get("mi_did")) for d in devices):
            raise RuntimeError("已有配置指定的音箱不在当前账号下，请检查 mi_did")
        print("复用已有配置，登录和设备检查通过。")
        return

    import yaml
    config = yaml.safe_load(template.read_text(encoding="utf-8"))
    while True:
        try:
            hostname = validate_address(ask("路由器局域网 IPv4 地址", "192.168.1.1"))
            check_local_address(hostname)
            break
        except (ValueError, OSError):
            print("地址不可用，请填写这台路由器自身的局域网 IPv4 地址。")
    config["account"] = ask("小米账号")
    config["password"] = ask("小米密码", secret=True)
    while True:
        try:
            config["api_base"] = validate_api_base(ask("API Base URL（通常以 /v1 结尾）"))
            break
        except ValueError as error:
            print(error)
    config["openai_key"] = ask("API Key", secret=True)
    config["gpt_options"]["model"] = ask("模型名称")
    print("\n默认使用免费 Edge 晓晓音色、流式播报；触发词：" + "、".join(config["keyword"]))
    # A cancelled first setup may have authenticated a different account.
    (directory / ".mi.token").unlink(missing_ok=True)
    device = choose_device(await login_devices(config, directory))
    config["mi_did"] = str(device["miotDID"])
    config["hardware"] = device["hardware"]
    from xiaogpt.config import HARDWARE_COMMAND_DICT
    if config["hardware"] not in HARDWARE_COMMAND_DICT:
        config["use_command"] = False
        print("该型号没有预设指令映射，将使用通用播报接口；实际音频播放仍需试听确认。")
    save_configuration(directory, config, hostname)
    print("配置已保存，权限为 600。API 信息尚未发送测试请求，请启动后实际提问验证。")


if __name__ == "__main__":
    try:
        asyncio.run(configure())
    except (KeyboardInterrupt, EOFError):
        print("\n安装配置已取消。", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # Report our own useful messages, never raw authentication responses.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"\n配置未完成：{message}", file=sys.stderr)
        sys.exit(1)
