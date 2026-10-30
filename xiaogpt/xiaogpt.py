#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import miservice
from aiohttp import ClientSession, ClientTimeout
from miservice import MiIOService, MiNAService, miio_command
from rich import print
from rich.logging import RichHandler

from xiaogpt.bot import get_bot
from xiaogpt.config import (
    COOKIE_TEMPLATE,
    LATEST_ASK_API,
    MI_ASK_SIMULATE_DATA,
    WAKEUP_KEYWORD,
    Config,
)
from xiaogpt.tts import TTS, MiTTS, TetosFileTTS
from xiaogpt.tts.live import TetosLiveTTS
from xiaogpt.tts.edge import EdgeLiveTTS
from xiaogpt.utils import detect_language, parse_cookie_string

EOF = object()
PASS_TOKEN_KEYS = ("deviceId", "userId", "passToken")

# Bare domains need a known suffix so names such as Node.js and filenames such
# as report.pdf are not removed. Explicit URL schemes accept any hostname.
_SPEECH_TLDS = (
    "com|org|net|edu|gov|mil|int|cn|中国|中國|公司|网络|網絡|在线|"
    "io|ai|app|dev|me|ink|sh|co|uk|us|de|fr|jp|kr|hk|tw|sg|au|ca|"
    "ru|in|it|es|nl|ch|se|no|fi|dk|nz|za|br|mx|cc|tv|eu|ly|gg|to|"
    "info|biz|xyz|top|site|online|tech|cloud|shop|store|wiki|news|"
    "link|live|club|vip|pro|name|museum|travel|mobi|asia|work|world|website|auto"
)
_SPEECH_DOMAIN = rf"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_SPEECH_TLDS})(?![a-z0-9-])"
_SPEECH_URL_CHAR = r"[^\s<>\[\]{}\"'，。！？；、（）()]"
_SPEECH_URL_TAIL = rf"(?:{_SPEECH_URL_CHAR}|\({_SPEECH_URL_CHAR}*\))*"
_SPEECH_HOST = r"(?:\[[0-9a-f:]+\]|[a-z0-9.-]+|[\w.-]+\.(?:中国|中國|公司|网络|網絡|在线))"
_SPEECH_URL = re.compile(
    rf"(?:(?:https?|ftp)://|www\.){_SPEECH_HOST}(?::\d+)?(?:[/?#]{_SPEECH_URL_TAIL})?"
    rf"|mailto:{_SPEECH_URL_TAIL}"
    rf"|(?<![a-z0-9_.@-]){_SPEECH_DOMAIN}(?::\d+)?(?:[/?#]{_SPEECH_URL_TAIL})?",
    re.IGNORECASE,
)
_SPEECH_MARKDOWN = re.compile(r"!?\[([^\]\n]*)\]\(")


def strip_speech_links(text: str) -> str:
    """Remove link destinations and citation-only labels, keeping human names."""
    text = re.sub(r"\ue200[^\ue201]*(?:\ue201|$)", "", text)
    start = 0
    parts = []
    while match := _SPEECH_MARKDOWN.search(text, start):
        parts.append(text[start:match.start()])
        end, depth = match.end(), 1
        while end < len(text) and depth:
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        label = match[1].strip()
        destination = text[match.end():end - 1 if not depth else end].strip()
        try:
            host = (urlsplit(destination.strip("<>")).hostname or "").lower()
        except ValueError:
            host = ""
        domain_label = label.lower().removeprefix("www.")
        labels_host = bool(host) and "." in domain_label and (
            host == domain_label or host.endswith("." + domain_label)
        )
        if not match[0].startswith("!") and not _SPEECH_URL.search(label) and not label.isdigit() and not labels_host:
            parts.append(label)
        start = end
    parts.append(text[start:])
    text = _SPEECH_URL.sub("", "".join(parts))
    text = re.sub(r"\[\d+(?:\s*[,，-]\s*\d+)*\]", "", text)
    # URL-only Markdown citations often leave nested empty parentheses.
    while True:
        cleaned = re.sub(r"\(\s*\)|\[\s*\]|（\s*）|【\s*】|<\s*>|`\s*`", "", text)
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"[ \t]+", " ", text)


async def filter_speech_links(text_stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Filter complete sentences without waiting for the whole model answer.

    Keep bracketed citations and URL query strings together even when the
    upstream sentence splitter emits a chunk ending in a URL's '?' or ';'.
    """
    pending = []
    brackets = []
    closing = {"(": ")", "[": "]", "<": ">", "（": "）", "【": "】", "\ue200": "\ue201"}
    token = ""
    deferred_exclamation = False
    async for chunk in text_stream:
        for char in chunk:
            # A leading '!' can be the start of a Markdown image, including
            # when it is separated from '[' by an upstream chunk boundary.
            if deferred_exclamation:
                deferred_exclamation = False
                if char != "[":
                    text = strip_speech_links("".join(pending))
                    if re.search(r"\w", text):
                        yield text
                    pending.clear()
                    token = ""
            pending.append(char)
            if char in closing:
                brackets.append(closing[char])
            elif brackets and char == brackets[-1]:
                brackets.pop()
            if char.isspace() or char in "，。！？；、（）":
                token = ""
            else:
                token += char
            is_end = char in "。！？；\n!?;"
            if char in "!?;" and _SPEECH_URL.search(token):
                is_end = False
            if char == "!" and is_end and not brackets:
                deferred_exclamation = True
                continue
            if is_end and not brackets:
                text = strip_speech_links("".join(pending))
                if re.search(r"\w", text):
                    yield text
                pending.clear()
                token = ""
    if pending:
        text = strip_speech_links("".join(pending))
        if re.search(r"\w", text):
            yield text


class MiAccount(miservice.MiAccount):
    """Log in again with the last passToken before falling back to the password.

    MiService drops the whole token when a request is rejected, so its relogin
    would start from a new deviceId and may ask for a verification code again.
    """

    pass_token: dict | None = None

    async def mi_request(self, sid, url, data, headers, relogin=True):
        await self._load_token()
        return await super().mi_request(sid, url, data, headers, relogin)

    async def login(self, sid):
        await self._load_token()
        if not self.token and self.pass_token:
            self.token = dict(self.pass_token)
        return await super().login(sid)

    async def _load_token(self):
        if self.token is None and self.token_store:
            self.token = await self.token_store.load_token()
        if self.token and all(k in self.token for k in PASS_TOKEN_KEYS):
            self.pass_token = {k: self.token[k] for k in PASS_TOKEN_KEYS}


class MiGPT:
    def __init__(self, config: Config):
        self.config = config

        self.mi_token_home = Path(
            os.getenv("XIAOGPT_MI_TOKEN_PATH", Path.home() / ".mi.token")
        )
        self.last_timestamp = int(time.time() * 1000)  # timestamp last call mi speaker
        self.cookie_jar = None
        self.device_id = ""
        self.mina_service = None
        self.miio_service = None
        self.in_conversation = False
        self.last_record = asyncio.Queue(1)
        # setup logger
        self.log = logging.getLogger("xiaogpt")
        self.log.setLevel(logging.DEBUG if config.verbose else logging.INFO)
        self.log.addHandler(RichHandler())
        self.log.debug(config)
        self.mi_session = ClientSession()

    async def close(self):
        await self.mi_session.close()

    async def poll_latest_ask(self):
        async with ClientSession() as session:
            session._cookie_jar = self.cookie_jar
            log_polling = int(self.config.verbose) > 1
            while True:
                if log_polling:
                    self.log.debug(
                        "Listening new message, timestamp: %s", self.last_timestamp
                    )
                await self.get_latest_ask_from_xiaoai(session)
                # Keep listening through generation AND playback, at the same
                # one-second minimum interval between cloud requests.
                await asyncio.sleep(1)

    async def init_all_data(self, refresh_token: bool = False):
        await self.login_miboy(refresh_token)
        await self._init_data_hardware()
        self.mi_session.cookie_jar.update_cookies(self.get_cookie())
        self.cookie_jar = self.mi_session.cookie_jar
        self.tts  # init tts

    async def login_miboy(self, refresh_token: bool = False):
        account = MiAccount(
            self.mi_session,
            self.config.account,
            self.config.password,
            str(self.mi_token_home),
        )
        # MiService loads persisted tokens on first request and reauthenticates
        # only when they are missing or rejected by the service.
        if refresh_token:
            await account.login("micoapi")
        self.mina_service = MiNAService(account)
        self.miio_service = MiIOService(account)

    async def _init_data_hardware(self):
        if self.config.cookie:
            # if use cookie do not need init
            return
        hardware_data = await self.mina_service.device_list()
        # fix multi xiaoai problems we check did first
        # why we use this way to fix?
        # some videos and articles already in the Internet
        # we do not want to change old way, so we check if miotDID in `env` first
        # to set device id

        for h in hardware_data:
            if did := self.config.mi_did:
                if h.get("miotDID", "") == str(did):
                    self.device_id = h.get("deviceID")
                    break
                else:
                    continue
            if h.get("hardware", "") == self.config.hardware:
                self.device_id = h.get("deviceID")
                break
        else:
            raise Exception(
                f"we have no hardware: {self.config.hardware} please use `miservice mina` to check"
            )
        if not self.config.mi_did:
            devices = await self.miio_service.device_list()
            try:
                self.config.mi_did = next(
                    d["did"]
                    for d in devices
                    if d["model"].endswith(self.config.hardware.lower())
                )
            except StopIteration:
                raise Exception(
                    f"cannot find did for hardware: {self.config.hardware} "
                    "please set it via MI_DID env"
                )

    def get_cookie(self):
        if self.config.cookie:
            cookie_jar = parse_cookie_string(self.config.cookie)
            # set attr from cookie fix #134
            cookie_dict = cookie_jar.get_dict()
            self.device_id = cookie_dict["deviceId"]
            return cookie_jar
        else:
            # use the live token, the persisted one may be missing or stale
            token = self.mina_service.account.token
            cookie_string = COOKIE_TEMPLATE.format(
                device_id=self.device_id,
                service_token=token["micoapi"][1],
                user_id=token["userId"],
            )
            return parse_cookie_string(cookie_string)

    @functools.cached_property
    def chatbot(self):
        return get_bot(self.config)

    def _reset_chatbot(self):
        # A fresh instance also isolates late results from interrupted requests.
        # Recreate lazily so each backend starts with its own empty history.
        self.__dict__.pop("chatbot", None)

    async def simulate_xiaoai_question(self):
        data = MI_ASK_SIMULATE_DATA
        # Convert the data['data'] value from a string to a dictionary
        data_dict = json.loads(data["data"])
        # Get the first item in the records list
        record = data_dict["records"][0]
        # Replace the query and time values with user input
        record["query"] = input("Enter the new query: ")
        record["time"] = int(time.time() * 1000)
        # Convert the updated data_dict back to a string and update the data['data'] value
        data["data"] = json.dumps(data_dict)
        await asyncio.sleep(1)

        return data

    def need_ask_gpt(self, record):
        if not record:
            return False
        query = record.get("query", "")
        return (
            self.in_conversation
            and not query.startswith(WAKEUP_KEYWORD)
            or query.lower().startswith(tuple(w.lower() for w in self.config.keyword))
        )

    def need_change_prompt(self, record):
        query = record.get("query", "")
        return query.startswith(tuple(self.config.change_prompt_keyword))

    def _change_prompt(self, new_prompt):
        new_prompt = re.sub(
            rf"^({'|'.join(self.config.change_prompt_keyword)})", "", new_prompt
        )
        new_prompt = "以下都" + new_prompt
        print(f"Prompt from {self.config.prompt} change to {new_prompt}")
        self.config.prompt = new_prompt
        self.chatbot.change_prompt(new_prompt)

    async def get_latest_ask_from_xiaoai(self, session: ClientSession) -> dict | None:
        retries = 3
        for i in range(retries):
            try:
                timeout = ClientTimeout(total=15)
                r = await session.get(
                    LATEST_ASK_API.format(
                        hardware=self.config.hardware,
                        timestamp=str(int(time.time() * 1000)),
                    ),
                    timeout=timeout,
                )
            except Exception as e:
                self.log.warning(
                    "Execption when get latest ask from xiaoai: %s", str(e)
                )
                continue
            try:
                data = await r.json()
            except Exception:
                self.log.warning("get latest ask from xiaoai error, retry")
                if i == 1:
                    # tricky way to fix #282 #272 # if it is the third time we re init all data
                    print("Maybe outof date trying to re init it")
                    await self._retry()
            else:
                return await self._get_last_query(data)
        return None

    async def _retry(self):
        # the conversation API may reject a serviceToken that mina still
        # accepts, so renew it with passToken instead of reusing it
        await self.init_all_data(refresh_token=True)

    async def _get_last_query(self, data: dict) -> dict | None:
        if d := data.get("data"):
            records = json.loads(d).get("records")
            if not records:
                return None
            last_record = records[0]
            timestamp = last_record.get("time")
            if timestamp > self.last_timestamp and not self.last_record.full():
                if self.config.mute_xiaoai and self.need_ask_gpt(last_record):
                    # Pause before publishing the record, so a delayed command
                    # cannot interrupt the waiting prompt or the GPT answer.
                    try:
                        paused = await self.mina_service.player_pause(self.device_id)
                        if paused is False:
                            self.log.warning("Xiaoai did not accept the pause command")
                    except Exception:
                        self.log.warning(
                            "Could not pause xiaoai; continuing with the question"
                        )
                try:
                    self.last_record.put_nowait(last_record)
                    self.last_timestamp = timestamp
                    return last_record
                except asyncio.QueueFull:
                    pass
        return None

    async def do_tts(self, value):
        if not self.config.use_command:
            try:
                await self.mina_service.text_to_speech(self.device_id, value)
            except Exception:
                pass
        else:
            await miio_command(
                self.miio_service,
                self.config.mi_did,
                f"{self.config.tts_command} {value}",
            )

    async def _play_waiting_prompt(self):
        if self.config.tts != "edge":
            await self.do_tts(f"正在问{self.chatbot.name}请耐心等待")
            return

        # Finish the prompt before the answer reuses the audio player.
        # Bound this optional playback so a TTS outage cannot block GPT.
        async with asyncio.timeout(20):
            prompt = await self.tts.cache_prompt(
                "正在问 GPT，请耐心等待", self.mi_token_home.parent / "audio-cache"
            )
            await self.tts.play_file(prompt)
        self.log.info("Waiting prompt completed from cached Edge audio")

    @functools.cached_property
    def tts(self) -> TTS:
        if self.config.tts == "mi":
            return MiTTS(self.mina_service, self.device_id, self.config)
        elif self.config.tts == "edge":
            return EdgeLiveTTS(self.mina_service, self.device_id, self.config)
        elif self.config.tts == "fish":
            return TetosLiveTTS(self.mina_service, self.device_id, self.config)
        else:
            return TetosFileTTS(self.mina_service, self.device_id, self.config)

    async def wait_for_tts_finish(self):
        while True:
            if not await self.get_if_xiaoai_is_playing():
                break
            await asyncio.sleep(1)

    @staticmethod
    def _normalize(message: str) -> str:
        message = message.strip().replace(" ", "--")
        message = message.replace("\n", "，")
        message = message.replace('"', "，")
        message = message.replace("*", "")
        return message

    async def ask_gpt(self, query: str) -> AsyncIterator[str]:
        if not self.config.stream:
            if self.config.bot == "glm":
                answer = self.chatbot.ask(query, **self.config.gpt_options)
            else:
                answer = await self.chatbot.ask(query, **self.config.gpt_options)
            message = answer if answer else ""
            yield message
            return

        queue = asyncio.Queue()
        chatbot = self.chatbot

        async def collect_stream():
            try:
                async with contextlib.aclosing(
                    chatbot.ask_stream(query, **self.config.gpt_options)
                ) as stream:
                    async for message in stream:
                        await queue.put(message)
            finally:
                queue.put_nowait(EOF)

        task = asyncio.create_task(collect_stream())
        try:
            is_eof = False
            while not is_eof:
                message = await queue.get()
                if message is EOF:
                    break
                while not queue.empty():
                    if (next_msg := queue.get_nowait()) is EOF:
                        is_eof = True
                        break
                    message += next_msg
                if message:
                    yield message
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _answer_with_waiting_prompt(self, query: str):
        queue = asyncio.Queue()
        started = time.monotonic()

        async def collect():
            first = True
            self.log.info("Starting GPT request alongside waiting prompt")
            try:
                async with contextlib.aclosing(self.ask_gpt(query)) as stream:
                    async for part in stream:
                        if first:
                            self.log.info(
                                "GPT first text ready after %.2f seconds",
                                time.monotonic() - started,
                            )
                            first = False
                        queue.put_nowait(part)
            finally:
                queue.put_nowait(EOF)

        producer = asyncio.create_task(collect())

        async def buffered():
            while True:
                part = await queue.get()
                if part is EOF:
                    break
                yield part
            await producer

        try:
            await asyncio.sleep(0)
            try:
                await self._play_waiting_prompt()
            except Exception as e:
                self.log.warning(
                    "Could not play waiting prompt; continuing with %s answer: %s",
                    getattr(self.chatbot, "name", "GPT"),
                    e,
                )
            async with contextlib.aclosing(buffered()) as stream:
                await self.speak(stream)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def _speak_until_interrupted(
        self, query: str, *, with_prompt: bool = False
    ) -> dict | None:
        async def answer():
            if with_prompt:
                await self._answer_with_waiting_prompt(query)
                return
            async with contextlib.aclosing(self.ask_gpt(query)) as stream:
                await self.speak(stream)

        answer_task = asyncio.create_task(answer())
        record_task = asyncio.create_task(self.last_record.get())
        interrupted_at = None
        try:
            while True:
                await asyncio.wait(
                    (answer_task, record_task), return_when=asyncio.FIRST_COMPLETED
                )
                # Prefer an arrived question even if playback ended in the same
                # event-loop turn, so it cannot be consumed and lost in cleanup.
                if record_task.done():
                    record = record_task.result()
                    text = record.get("query", "").strip()
                    if (
                        self.need_ask_gpt(record)
                        or self.need_change_prompt(record)
                        or text in (
                            self.config.start_conversation,
                            self.config.end_conversation,
                        )
                    ):
                        interrupted_at = time.monotonic()
                        self.log.info("New request received; closing previous answer")
                        return record
                    # Native Xiaoai commands do not become GPT questions.
                    record_task = asyncio.create_task(self.last_record.get())
                if answer_task.done():
                    await answer_task
                    return None
        finally:
            record_task.cancel()
            answer_task.cancel()
            # TTS must close its URL and finish stopping the old track before
            # the next waiting prompt or playback command can be sent.
            await asyncio.gather(record_task, answer_task, return_exceptions=True)
            if interrupted_at is not None:
                self.log.info(
                    "Previous answer closed in %.2f seconds",
                    time.monotonic() - interrupted_at,
                )

    async def get_if_xiaoai_is_playing(self):
        playing_info = await self.mina_service.player_get_status(self.device_id)
        return (playing_info or {}).get("status") == 1

    async def stop_if_xiaoai_is_playing(self):
        is_playing = await self.get_if_xiaoai_is_playing()
        if is_playing:
            self.log.debug("Muting xiaoai")
            # stop it
            await self.mina_service.player_pause(self.device_id)

    async def wakeup_xiaoai(self):
        return await miio_command(
            self.miio_service,
            self.config.mi_did,
            f"{self.config.wakeup_command} {WAKEUP_KEYWORD} 0",
        )

    async def run_forever(self):
        await self.init_all_data()
        task = asyncio.create_task(self.poll_latest_ask())
        try:
            await self._run_conversation_loop()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_conversation_loop(self):
        print(
            f"Running xiaogpt now, 用 [green]{'/'.join(self.config.keyword)}[/] 开头来提问"
        )
        print(f"或用 [green]{self.config.start_conversation}[/] 开始持续对话")
        pending_record = None
        while True:
            new_record = pending_record or await self.last_record.get()
            pending_record = None
            query = new_record.get("query", "").strip()
            if query == self.config.start_conversation:
                if not self.in_conversation:
                    print("开始对话")
                    self._reset_chatbot()
                    self.in_conversation = True
                    await self.wakeup_xiaoai()
                await self.stop_if_xiaoai_is_playing()
                continue
            elif query == self.config.end_conversation:
                if self.in_conversation:
                    print("结束对话")
                    self.in_conversation = False
                    self._reset_chatbot()
                await self.stop_if_xiaoai_is_playing()
                continue

            # we can change prompt
            if self.need_change_prompt(new_record):
                print(new_record)
                self._change_prompt(new_record.get("query", ""))

            if not self.need_ask_gpt(new_record):
                self.log.debug("No new xiao ai record")
                continue

            if not self.in_conversation:
                self._reset_chatbot()

            # Trigger phrases can carry the task itself (e.g. "请对比").
            # They gate activation above, but must remain in the model query.
            # llama3 is not good at Chinese, so we need to add prompt in it.
            if self.config.bot == "llama":
                query = f"你是一个基于 llama3 的智能助手，请你跟我对话时，一定使用中文，不要夹杂一些英文单词，甚至英语短语也不能随意使用，但类似于 llama3 这样的专属名词除外，问题是：{query}"

            print("-" * 20)
            print("问题：" + query + "？")
            if not self.chatbot.has_history():
                query = f"{query},{self.config.prompt}"
            # some model can not detect the language code, so we need to add it

            if not self.config.mute_xiaoai:
                # waiting for xiaoai speaker done
                await asyncio.sleep(8)
            try:
                print(
                    "以下是小爱的回答：",
                    new_record.get("answers", [])[0].get("tts", {}).get("text"),
                )
            except IndexError:
                print("小爱没回")
            print(f"以下是 {self.chatbot.name} 的回答：", end="")
            try:
                pending_record = await self._speak_until_interrupted(query, with_prompt=True)
            except Exception as e:
                print(f"{self.chatbot.name} 回答出错 {str(e)}")
            else:
                if pending_record is None:
                    print("回答完毕")
            if self.in_conversation and pending_record is None:
                print(f"继续对话，或用 `{self.config.end_conversation}` 结束对话")
                await self.wakeup_xiaoai()

    async def speak(self, text_stream: AsyncIterator[str]) -> None:
        async def spoken_text():
            async for text in filter_speech_links(text_stream):
                yield self._normalize(text)

        filtered_stream = spoken_text()
        first_chunk = await anext(filtered_stream, None)
        if first_chunk is None:
            return
        # Detect the language from the first chunk
        # Add suffix '-' because tetos expects it to exist when selecting voices
        # however, the nation code is never used.
        lang = detect_language(first_chunk) + "-"

        async def gen():  # reconstruct the generator
            yield first_chunk
            async for text in filtered_stream:
                yield text

        await self.tts.synthesize(lang, gen())
