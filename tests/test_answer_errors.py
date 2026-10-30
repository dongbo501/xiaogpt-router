"""API failures stay audible, interruptible, and out of conversation history."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai

from xiaogpt.bot.chatgptapi_bot import ChatGPTBot
from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT


def rate_limit(code="rate_limit_exceeded"):
    return openai.RateLimitError(
        "Rate limit reached for tokens per minute; please retry shortly",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        ),
        body={"code": code},
    )


def event(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class ChatGPTErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = ChatGPTBot(openai_key="test")
        self.create = AsyncMock()
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        )

    async def collect(self):
        return [part async for part in self.bot.ask_stream("question")]

    async def test_initial_http_errors_propagate_in_both_modes(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.create.side_effect = rate_limit()
                with patch.object(self.bot, "_make_openai_client", return_value=self.client):
                    with self.assertRaises(openai.RateLimitError):
                        await (self.collect() if stream else self.bot.ask("question"))
                self.assertEqual(self.bot.history, [])

    async def test_stream_error_before_text_propagates(self):
        async def events():
            raise RuntimeError("Rate limit reached for tokens per minute")
            yield

        self.create.return_value = events()
        with patch.object(self.bot, "_make_openai_client", return_value=self.client):
            with self.assertRaisesRegex(RuntimeError, "Rate limit"):
                await self.collect()
        self.assertEqual(self.bot.history, [])

    async def test_http_200_sse_error_from_provider_reaches_audio_handler(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"error":{"type":"upstream_error",'
                '"message":"Rate limit reached for tokens per minute; please retry shortly"}}\n\n'
                'data: [DONE]\n\n'
            ),
        ))
        async with httpx.AsyncClient(transport=transport) as session:
            client = openai.AsyncOpenAI(
                api_key="test", base_url="https://example.invalid/v1",
                max_retries=0, http_client=session,
            )
            with patch.object(self.bot, "_make_openai_client", return_value=client):
                with self.assertRaisesRegex(openai.APIError, "tokens per minute"):
                    await self.collect()
        self.assertEqual(self.bot.history, [])

    async def test_partial_failed_stream_is_not_added_to_history(self):
        async def events():
            yield event("第一句话。")
            raise RuntimeError("stream interrupted")

        self.create.return_value = events()
        with patch.object(self.bot, "_make_openai_client", return_value=self.client):
            stream = self.bot.ask_stream("question")
            self.assertEqual(await anext(stream), "第一句话。")
            with self.assertRaisesRegex(RuntimeError, "stream interrupted"):
                await anext(stream)
        self.assertEqual(self.bot.history, [])

    async def test_empty_answers_are_failures_in_both_modes(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                async def events():
                    yield event(None)
                    yield event("   ")

                self.create.return_value = events() if stream else SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
                )
                with patch.object(self.bot, "_make_openai_client", return_value=self.client):
                    with self.assertRaisesRegex(RuntimeError, "empty answer"):
                        await (self.collect() if stream else self.bot.ask("question"))
                self.assertEqual(self.bot.history, [])

    async def test_cancelled_stream_keeps_history_clean(self):
        started = asyncio.Event()

        async def events():
            yield event("第一句话。")
            started.set()
            await asyncio.Future()

        self.create.return_value = events()
        with patch.object(self.bot, "_make_openai_client", return_value=self.client):
            task = asyncio.create_task(self.collect())
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.bot.history, [])

    async def test_completed_stream_is_added_to_history(self):
        async def events():
            yield event("测试成功。")

        self.create.return_value = events()
        with patch.object(self.bot, "_make_openai_client", return_value=self.client):
            self.assertEqual("".join(await self.collect()), "测试成功。")
        self.assertEqual(self.bot.history, [["question", "测试成功。"]])


class AnswerErrorAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MiGPT(Config(openai_key="test", tts="edge", stream=True, keyword=["请问"]))
        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(side_effect=lambda text, directory: text),
            play_file=AsyncMock(),
        )
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bot.close()
        for handler in self.bot.log.handlers[:]:
            self.bot.log.removeHandler(handler)

    async def consume(self, stream):
        async for _ in stream:
            pass

    async def test_rate_limit_speaks_after_waiting_prompt_without_retry(self):
        prompt_done = asyncio.Event()
        played = []

        async def play(text):
            if text == "正在问 GPT，请耐心等待":
                await asyncio.sleep(0.01)
                prompt_done.set()
            else:
                self.assertTrue(prompt_done.is_set())
            played.append(text)

        async def model(query):
            raise rate_limit()
            yield

        self.bot.tts.play_file.side_effect = play
        with (
            patch.object(self.bot, "ask_gpt", side_effect=model) as ask,
            patch.object(self.bot, "speak", side_effect=self.consume),
            patch.object(self.bot, "do_tts", new_callable=AsyncMock) as native,
        ):
            with self.assertRaises(openai.RateLimitError):
                await asyncio.wait_for(self.bot._speak_until_interrupted("test", with_prompt=True), 1)
        self.assertEqual(played, ["正在问 GPT，请耐心等待", "GPT 服务暂时限流，请稍后再试。"])
        self.assertEqual(ask.call_count, 1)
        native.assert_not_awaited()
        self.assertTrue(all(
            call.args[1] == self.bot.mi_token_home.parent / "audio-cache"
            for call in self.bot.tts.cache_prompt.call_args_list
        ))

    async def test_stream_error_text_and_quota_have_specific_prompts(self):
        for error, expected in (
            (RuntimeError("Rate limit reached for tokens per minute"), "GPT 服务暂时限流，请稍后再试。"),
            (rate_limit("insufficient_quota"), "GPT 服务额度不足，请检查接口账户。"),
            (RuntimeError("upstream unavailable"), "GPT 暂时无法回答，请稍后再试。"),
        ):
            with self.subTest(error=error):
                await self.bot._play_answer_error(error)
                self.bot.tts.play_file.assert_awaited_with(expected)

    async def test_error_audio_failure_preserves_original_error(self):
        error = rate_limit()

        async def model(query):
            raise error
            yield

        self.bot.tts.play_file.side_effect = TimeoutError("speaker offline")
        with (
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=self.consume),
            self.assertLogs("xiaogpt", level="WARNING"),
        ):
            with self.assertRaises(openai.RateLimitError) as caught:
                await asyncio.wait_for(self.bot._speak_until_interrupted("test"), 1)
        self.assertIs(caught.exception, error)

    async def test_new_question_interrupts_error_audio(self):
        playing, closed = asyncio.Event(), asyncio.Event()

        async def model(query):
            raise rate_limit()
            yield

        async def play(text):
            playing.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        self.bot.tts.play_file.side_effect = play
        with (
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=self.consume),
        ):
            task = asyncio.create_task(self.bot._speak_until_interrupted("old"))
            self.tasks.append(task)
            await asyncio.wait_for(playing.wait(), 1)
            record = {"query": "请问新问题"}
            await self.bot.last_record.put(record)
            self.assertEqual(await asyncio.wait_for(task, 1), record)
        self.assertTrue(closed.is_set())

    async def test_cancellation_does_not_play_failure_prompt(self):
        started = asyncio.Event()

        async def model(query):
            started.set()
            await asyncio.Future()
            yield

        with (
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=self.consume),
        ):
            task = asyncio.create_task(self.bot._speak_until_interrupted("old"))
            self.tasks.append(task)
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.bot.tts.cache_prompt.assert_not_awaited()

    async def test_conversation_accepts_next_question_after_failure(self):
        completed = asyncio.Queue()
        answers = []
        original = self.bot._speak_until_interrupted

        async def model(query):
            if "第一题" in query:
                raise rate_limit()
            yield "测试成功。"

        async def speak(stream):
            async for part in stream:
                answers.append(part)

        async def observe(query, **kwargs):
            try:
                return await original(query, **kwargs)
            finally:
                completed.put_nowait(query)

        self.bot.config.mute_xiaoai = True
        with (
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=speak),
            patch.object(self.bot, "_speak_until_interrupted", side_effect=observe),
        ):
            task = asyncio.create_task(self.bot._run_conversation_loop())
            self.tasks.append(task)
            for question in ("请问第一题", "请问第二题"):
                await self.bot.last_record.put({"query": question})
                await asyncio.wait_for(completed.get(), 1)
            self.assertFalse(task.done())
        self.assertEqual(answers, ["测试成功。"])
        self.assertEqual(self.bot.tts.play_file.await_count, 3)


if __name__ == "__main__":
    unittest.main()
