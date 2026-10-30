"""Validated configuration edits; credentials never leave the server."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from xiaogpt.config import Config, HARDWARE_COMMAND_DICT


class ValidationError(ValueError):
    pass


class ConflictError(ValidationError):
    pass


PUBLIC_FIELDS = (
    "api_base", "gpt_options", "prompt", "waiting_prompt", "rate_limit_prompt",
    "quota_prompt", "error_prompt", "keyword", "stream", "mute_xiaoai",
    "tts", "tts_options", "hardware", "mi_did", "use_command",
    "start_conversation", "end_conversation",
)
PROMPT_FIELDS = ("prompt", "waiting_prompt", "rate_limit_prompt", "quota_prompt", "error_prompt")
PROVIDERS = [
    {"name": "EasyCoding", "url": "https://api.easycoding.me/v1"},
    {"name": "String", "url": "https://www.string.ink/v1"},
    {"name": "OpenAI", "url": "https://api.openai.com/v1"},
    {"name": "自定义兼容接口", "url": ""},
]
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 · 普通话女声"},
    {"id": "zh-CN-YunxiNeural", "name": "云希 · 普通话男声"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊 · 普通话女声"},
    {"id": "zh-CN-YunjianNeural", "name": "云健 · 普通话男声"},
    {"id": "zh-HK-HiuMaanNeural", "name": "晓曼 · 粤语女声"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "晓臻 · 台湾女声"},
]


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".web-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def api_base(value):
    if not isinstance(value, str):
        raise ValidationError("请填写接口地址")
    value = value.strip().rstrip("/")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValidationError("接口地址格式不正确") from None
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValidationError("接口地址须以 http:// 或 https:// 开头")
    if url.username or url.password or url.query or url.fragment or any(c.isspace() for c in value):
        raise ValidationError("地址中不能包含密钥、账号、空格或查询参数")
    # Accept the base URL people normally copy from a provider's dashboard.
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if not path:
        path = "/v1"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent
        self.profiles_path = self.directory / "web-profiles.json"
        self.backup_dir = self.directory / "web-backups"
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.profiles_path.exists():
            current = self.read()
            write_json(self.profiles_path, [{
                "id": "initial", "name": "当前可用接口", "api_base": current.get("api_base") or "https://api.openai.com/v1",
                "openai_key": current.get("openai_key", ""),
                "model": current.get("gpt_options", {}).get("model", "gpt-4o-mini"),
            }])

    def read(self):
        return Config.read_from_file(str(self.path))

    def revision(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def profiles(self):
        return json.loads(self.profiles_path.read_text())

    def public(self):
        current = self.read()
        defaults = Config.__dataclass_fields__
        result = {key: copy.deepcopy(current[key]) for key in PUBLIC_FIELDS if key in current}
        for key in PROMPT_FIELDS:
            result.setdefault(key, defaults[key].default)
        result.setdefault("gpt_options", {})
        result.setdefault("tts_options", {})
        result["tts_options"].pop("proxy", None)
        profiles = self.profiles()
        active = next((p["id"] for p in profiles if (
            p["api_base"] == current.get("api_base") and p["openai_key"] == current.get("openai_key")
        )), "")
        return {
            "config": result, "revision": self.revision(), "key_saved": bool(current.get("openai_key")),
            "profiles": [{k: v for k, v in p.items() if k != "openai_key"} | {"key_saved": bool(p.get("openai_key"))} for p in profiles],
            "active_profile": active, "providers": PROVIDERS, "voices": VOICES,
            "hardware": sorted(HARDWARE_COMMAND_DICT), "config_file": str(self.path),
        }

    def connection(self, body):
        current = self.read()
        selected = body.get("profile_id", "")
        profile = next((p for p in self.profiles() if p["id"] == selected), None)
        if selected and profile is None:
            raise ValidationError("接口方案不存在，请刷新页面")
        source = profile or current
        address = api_base(body.get("api_base", source.get("api_base") or "https://api.openai.com/v1"))
        key = body.get("openai_key", "")
        if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key):
            raise ValidationError("API Key 不能包含空格或换行")
        if not key:
            if address != api_base(source.get("api_base") or "https://api.openai.com/v1"):
                raise ValidationError("更换接口地址后，请填写该接口对应的 API Key，或选择已保存方案")
            key = source.get("openai_key", "")
        if not key or key.lower().startswith("bearer "):
            raise ValidationError("请填写有效的 API Key，不要带 Bearer 前缀")
        model = body.get("model", source.get("model") or current.get("gpt_options", {}).get("model", ""))
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValidationError("请选择或填写模型名称")
        return {"api_base": address, "openai_key": key, "model": model.strip()}

    def candidate(self, body):
        current = self.read()
        candidate = copy.deepcopy(current)
        values = body.get("config", {})
        if not isinstance(values, dict) or set(values) - set(PUBLIC_FIELDS):
            raise ValidationError("设置包含不支持的字段")
        candidate.update(values)
        if "tts_options" in values and isinstance(values["tts_options"], dict):
            candidate["tts_options"] = {**current.get("tts_options", {}), **values["tts_options"]}
        connection = self.connection({
            **body, "api_base": candidate.get("api_base"),
            "model": candidate.get("gpt_options", {}).get("model", ""),
        })
        candidate.update({k: connection[k] for k in ("api_base", "openai_key")})
        for key in PROMPT_FIELDS:
            value = candidate.get(key, Config.__dataclass_fields__[key].default)
            if not isinstance(value, str) or len(value) > (16000 if key == "prompt" else 200):
                raise ValidationError("提示词或提示语过长，或格式不正确")
        for key in ("stream", "mute_xiaoai", "use_command"):
            if type(candidate.get(key, False)) is not bool:
                raise ValidationError("开关设置格式不正确")
        keywords = candidate.get("keyword", [])
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 30 or any(not isinstance(k, str) or not k.strip() or len(k) > 50 for k in keywords):
            raise ValidationError("请设置 1–30 个触发词，每个不超过 50 字")
        candidate["keyword"] = list(dict.fromkeys(k.strip() for k in keywords))
        for key in ("start_conversation", "end_conversation"):
            if not isinstance(candidate.get(key), str) or not 1 <= len(candidate[key].strip()) <= 50:
                raise ValidationError("持续对话口令不能为空，且不能超过 50 字")
        if candidate["start_conversation"] == candidate["end_conversation"]:
            raise ValidationError("开始和结束对话的口令不能相同")
        if candidate.get("tts") not in ("edge", "mi"):
            raise ValidationError("此管理页支持 Edge 或小爱原声")
        options = candidate.get("tts_options", {})
        if not isinstance(options, dict) or set(options) - {"voice", "rate", "volume", "pitch", "proxy"}:
            raise ValidationError("语音参数格式不正确")
        if "voice" in options and (not isinstance(options["voice"], str) or not re.fullmatch(r"[A-Za-z0-9-]{3,100}", options["voice"])):
            raise ValidationError("请填写正确的 Edge 音色名称")
        for key, suffix, lower, upper in (("rate", "%", -50, 100), ("volume", "%", -100, 100), ("pitch", "Hz", -50, 50)):
            value = options.get(key, "+0" + suffix)
            if not isinstance(value, str) or not re.fullmatch(r"[+-]\d+" + suffix, value) or not lower <= int(value[:-len(suffix)]) <= upper:
                raise ValidationError(f"语音 {key} 参数超出范围")
        if candidate.get("hardware") not in HARDWARE_COMMAND_DICT or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(candidate.get("mi_did", ""))):
            raise ValidationError("请选择音箱型号，并填写有效的设备 DID")
        options = candidate.get("gpt_options", {})
        if not isinstance(options, dict) or set(options) - {"model", "temperature", "max_tokens", "max_completion_tokens", "top_p", "reasoning_effort"}:
            raise ValidationError("高级模型参数包含不支持的字段")
        options["model"] = connection["model"]
        for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("max_tokens", 1, 1000000), ("max_completion_tokens", 1, 1000000)):
            if key in options and (type(options[key]) not in (int, float) or not low <= options[key] <= high):
                raise ValidationError(f"模型参数 {key} 超出范围")
            if key in ("max_tokens", "max_completion_tokens") and key in options and type(options[key]) is not int:
                raise ValidationError("Token 上限必须是整数")
        if "reasoning_effort" in options and options["reasoning_effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh"):
            raise ValidationError("推理强度不受支持")
        candidate["gpt_options"] = options
        Config(**candidate)
        return candidate

    def backup(self):
        self.backup_dir.mkdir(mode=0o700, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        suffix = self.path.suffix
        target = self.backup_dir / (name + suffix)
        atomic_write(target, self.path.read_text())
        for old in sorted(self.backup_dir.iterdir(), reverse=True)[20:]:
            old.unlink()
        return target.name

    def save(self, body):
        if body.get("revision") != self.revision():
            raise ConflictError("配置已被其他页面或终端修改，请先刷新，避免覆盖")
        candidate = self.candidate(body)
        self.backup()
        if self.path.suffix == ".json":
            write_json(self.path, candidate)
        else:
            import yaml
            atomic_write(self.path, yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False))
        return self.public()

    def save_profile(self, body):
        connection = self.connection(body)
        name = body.get("name", "").strip()
        if not name or len(name) > 50:
            raise ValidationError("请为接口方案填写 1–50 字的名称")
        profiles = self.profiles()
        if len(profiles) >= 20:
            raise ValidationError("最多保存 20 个接口方案")
        profile = {"id": uuid.uuid4().hex, "name": name, **connection}
        profiles.append(profile)
        write_json(self.profiles_path, profiles)
        return profile["id"]

    def delete_profile(self, identifier):
        profiles = self.profiles()
        if not any(p["id"] == identifier for p in profiles):
            raise ValidationError("接口方案不存在")
        write_json(self.profiles_path, [p for p in profiles if p["id"] != identifier])

    def backups(self):
        return [{"id": p.name, "modified": p.stat().st_mtime} for p in sorted(self.backup_dir.glob("*"), reverse=True) if p.is_file()]

    def restore(self, identifier, revision):
        if revision != self.revision():
            raise ConflictError("配置已经变化，请刷新后再恢复")
        if identifier not in {b["id"] for b in self.backups()}:
            raise ValidationError("备份不存在")
        data = Config.read_from_file(str(self.backup_dir / identifier))
        Config(**data)
        self.backup()
        if self.path.suffix == ".json":
            write_json(self.path, data)
        else:
            import yaml
            atomic_write(self.path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
