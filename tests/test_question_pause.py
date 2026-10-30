"""Keep the pause ahead of the waiting prompt without extra status requests."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT


class QuestionPauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MiGPT(
            Config(
                account="test",
                password="test",
                openai_key="test",
                hardware="L05C",
                mi_did="123",
                keyword=["请问"],
                mute_xiaoai=True,
            )
        )
        self.bot.device_id = "speaker"
        self.bot.mina_service = SimpleNamespace(
            player_get_status=AsyncMock(),
            player_pause=AsyncMock(return_value=True),
            text_to_speech=AsyncMock(return_value=True),
        )

    async def asyncTearDown(self):
        await self.bot.close()

    def record(self, query="请问太阳为什么发光"):
        return {"query": query, "time": self.bot.last_timestamp + 1}

    def response(self, record):
        return {"data": json.dumps({"records": [record]})}

    async def test_pause_finishes_before_prompt_and_is_not_repeated(self):
        events = []
        record = self.record()
        pause_started, allow_pause = asyncio.Event(), asyncio.Event()

        async def pause(device):
            self.assertEqual(device, "speaker")
            pause_started.set()
            await allow_pause.wait()
            events.append("pause")
            return True

        async def poll():
            session = SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        json=AsyncMock(return_value=self.response(record))
                    )
                )
            )
            await self.bot.get_latest_ask_from_xiaoai(session)
            # Xiaomi returns the same latest record on subsequent polls.
            self.assertIsNone(await self.bot._get_last_query(self.response(record)))

        async def prompt(text):
            events.append(text)

        async def speak(stream):
            events.append("answer")
            raise asyncio.CancelledError

        async def model(query):
            yield "test answer"

        self.bot.mina_service.player_pause.side_effect = pause
        self.bot.chatbot = SimpleNamespace(has_history=lambda: True, name="ChatGPT")
        with (
            patch.object(self.bot, "init_all_data", new_callable=AsyncMock),
            patch.object(self.bot, "poll_latest_ask", side_effect=poll),
            patch.object(self.bot, "do_tts", side_effect=prompt),
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=speak),
        ):
            runner = asyncio.create_task(self.bot.run_forever())
            try:
                await asyncio.wait_for(pause_started.wait(), 2)
                self.assertTrue(self.bot.last_record.empty())
                self.assertEqual(events, [])
                allow_pause.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(runner, 2)
            finally:
                allow_pause.set()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

        self.assertEqual(events, ["pause", "正在问ChatGPT请耐心等待", "answer"])
        self.bot.mina_service.player_pause.assert_awaited_once_with("speaker")
        self.bot.mina_service.player_get_status.assert_not_awaited()

    async def test_normal_questions_and_disabled_muting_do_not_pause(self):
        for query, enabled in (("今天天气怎么样", True), ("请问太阳", False)):
            with self.subTest(query=query, enabled=enabled):
                self.bot.config.mute_xiaoai = enabled
                record = self.record(query)
                await self.bot._get_last_query(self.response(record))
                self.assertEqual(self.bot.last_record.get_nowait(), record)
        self.bot.mina_service.player_pause.assert_not_awaited()
        self.bot.mina_service.player_get_status.assert_not_awaited()

    async def test_continuous_conversation_also_pauses(self):
        self.bot.in_conversation = True
        record = self.record("继续解释")
        await self.bot._get_last_query(self.response(record))
        self.assertEqual(self.bot.last_record.get_nowait(), record)
        self.bot.mina_service.player_pause.assert_awaited_once_with("speaker")
        self.bot.mina_service.player_get_status.assert_not_awaited()

    async def test_full_queue_does_not_send_an_unusable_pause(self):
        self.bot.last_record.put_nowait(self.record())
        self.assertIsNone(await self.bot._get_last_query(self.response(self.record())))
        self.bot.mina_service.player_pause.assert_not_awaited()

    async def test_pause_failure_does_not_drop_the_question(self):
        for result in (False, RuntimeError("offline")):
            with self.subTest(result=result):
                if isinstance(result, Exception):
                    self.bot.mina_service.player_pause.side_effect = result
                else:
                    self.bot.mina_service.player_pause.return_value = result
                record = self.record()
                with self.assertLogs("xiaogpt", level="WARNING"):
                    await self.bot._get_last_query(self.response(record))
                self.assertEqual(self.bot.last_record.get_nowait(), record)

    async def test_waiting_prompt_failure_keeps_gpt_and_next_question_running(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        self.bot.miio_service = SimpleNamespace()
        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"),
            play_file=AsyncMock(side_effect=[
                RuntimeError("audio unavailable"), TimeoutError("cached prompt timed out")
            ]),
        )
        queries = ["请问第一题", "请问第二题"]
        received = []
        processed = asyncio.Event()

        async def poll():
            for query in queries:
                await self.bot.last_record.put(self.record(query))
                await processed.wait()
                processed.clear()

        async def answer(query):
            received.append(query)
            yield "test answer"

        async def speak(stream):
            async for _ in stream:
                pass
            processed.set()
            if len(received) == len(queries):
                raise asyncio.CancelledError

        with (
            patch.object(self.bot, "init_all_data", new_callable=AsyncMock),
            patch.object(self.bot, "poll_latest_ask", side_effect=poll),
            patch(
                "xiaogpt.xiaogpt.miio_command",
                new_callable=AsyncMock,
                side_effect=AssertionError("Edge prompts must not call Xiaomi TTS"),
            ) as command,
            patch.object(self.bot, "ask_gpt", side_effect=answer),
            patch.object(self.bot, "speak", side_effect=speak),
            self.assertLogs("xiaogpt", level="WARNING") as logs,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.bot.run_forever(), 2)

        self.assertEqual(received, [q + "," + self.bot.config.prompt for q in queries])
        command.assert_not_awaited()
        self.assertEqual(self.bot.tts.play_file.await_count, 2)
        self.assertEqual(sum(
            "continuing with ChatGPT answer" in line for line in logs.output
        ), 2)

    async def test_waiting_prompt_does_not_swallow_cancellation(self):
        self.bot.config.use_command = True
        self.bot.miio_service = SimpleNamespace()
        closed = asyncio.Event()

        async def model(query):
            try:
                await asyncio.Future()
                yield "unreachable"
            finally:
                closed.set()

        with (
            patch(
                "xiaogpt.xiaogpt.miio_command",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", new_callable=AsyncMock) as answer,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.bot._answer_with_waiting_prompt("test"), 2)
        answer.assert_not_awaited()
        self.assertTrue(closed.is_set())
        self.bot.mina_service.text_to_speech.assert_not_awaited()

    async def test_edge_waiting_prompt_skips_xiaomi_tts_even_when_it_would_work(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        self.bot.miio_service = SimpleNamespace()
        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"), play_file=AsyncMock()
        )
        with (
            patch("xiaogpt.xiaogpt.miio_command", new_callable=AsyncMock) as command,
            patch.object(self.bot, "do_tts", new_callable=AsyncMock) as native_tts,
        ):
            await self.bot._play_waiting_prompt()
        command.assert_not_awaited()
        native_tts.assert_not_awaited()
        self.bot.tts.play_file.assert_awaited_once_with("cached.mp3")
        self.bot.mina_service.text_to_speech.assert_not_awaited()

    async def test_edge_waiting_prompt_uses_requested_text(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        self.bot.miio_service = SimpleNamespace()
        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"), play_file=AsyncMock()
        )
        with (
            patch(
                "xiaogpt.xiaogpt.miio_command",
                new_callable=AsyncMock,
                side_effect=AssertionError("Edge prompts must not call Xiaomi TTS"),
            ) as command,
            self.assertLogs("xiaogpt", level="INFO") as logs,
        ):
            await self.bot._play_waiting_prompt()
        command.assert_not_awaited()
        self.bot.tts.cache_prompt.assert_awaited_once_with(
            "正在问 GPT，请耐心等待", self.bot.mi_token_home.parent / "audio-cache"
        )
        self.bot.tts.play_file.assert_awaited_once_with("cached.mp3")
        self.bot.mina_service.text_to_speech.assert_not_awaited()
        self.assertTrue(any("completed from cached Edge audio" in line for line in logs.output))

    async def test_edge_waiting_prompt_has_bounded_wait(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        closed = asyncio.Event()

        async def stalled_speech(*args):
            try:
                await asyncio.Future()
            finally:
                closed.set()

        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"),
            play_file=AsyncMock(side_effect=stalled_speech),
        )
        real_timeout = asyncio.timeout
        with (
            patch.object(self.bot, "do_tts", side_effect=RuntimeError("rejected")),
            patch(
                "xiaogpt.xiaogpt.asyncio.timeout",
                side_effect=lambda seconds: real_timeout(0.01),
            ) as timeout,
        ):
            with self.assertRaises(TimeoutError):
                await self.bot._play_waiting_prompt()
        timeout.assert_called_once_with(20)
        self.assertTrue(closed.is_set())

    async def test_edge_waiting_prompt_does_not_swallow_cancellation(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"),
            play_file=AsyncMock(side_effect=asyncio.CancelledError),
        )
        with (
            patch.object(self.bot, "do_tts", side_effect=RuntimeError("rejected")),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.bot._play_waiting_prompt()

    async def test_gpt_runs_during_prompt_but_answer_audio_waits(self):
        self.bot.config.use_command = True
        self.bot.config.tts = "edge"
        started, finish, answered, model_started = (asyncio.Event() for _ in range(4))
        events = []

        async def play_file(path):
            self.assertEqual(path, "cached.mp3")
            started.set()
            await finish.wait()
            events.append("prompt-completed")

        async def model(query):
            model_started.set()
            events.append("model-started")
            for part in ("first", "second", "third"):
                yield part

        async def speak(stream):
            self.assertEqual([part async for part in stream], ["first", "second", "third"])
            events.append("answer-audio")
            answered.set()

        self.bot.tts = SimpleNamespace(
            cache_prompt=AsyncMock(return_value="cached.mp3"),
            play_file=AsyncMock(side_effect=play_file),
        )
        self.bot.last_record.put_nowait(self.record())
        with (
            patch.object(self.bot, "do_tts", side_effect=RuntimeError("rejected")),
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=speak) as response,
        ):
            runner = asyncio.create_task(self.bot._run_conversation_loop())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(model_started.wait(), 2)
                response.assert_not_awaited()
                finish.set()
                await asyncio.wait_for(answered.wait(), 2)
                self.assertFalse(runner.done())
                self.assertEqual(events, ["model-started", "prompt-completed", "answer-audio"])
            finally:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    async def test_trigger_phrases_keep_the_task_when_switching_topics(self):
        self.bot.config.keyword = ["请问", "请对比", "请详细向我解释", "补充"]
        self.bot.config.prompt = "test speech rules"
        queries = [
            "请问国家税务总局官网怎么找",
            "请对比蜜雪冰城和茶白二",
            "请对比特斯拉和华为汽车",
            "请详细向我解释红外线遥控器的原理",
            "补充，我更在乎价格",
        ]
        received = []
        processed = asyncio.Event()
        self.bot.chatbot = SimpleNamespace(
            has_history=lambda: bool(received), name="ChatGPT"
        )

        async def poll():
            for query in queries:
                await self.bot.last_record.put(self.record(query))
                await processed.wait()
                processed.clear()

        async def answer(query):
            received.append(query)
            yield "test answer"

        async def speak(stream):
            async for _ in stream:
                pass
            processed.set()
            if len(received) == len(queries):
                raise asyncio.CancelledError

        with (
            patch.object(self.bot, "init_all_data", new_callable=AsyncMock),
            patch.object(self.bot, "poll_latest_ask", side_effect=poll),
            patch.object(self.bot, "do_tts", new_callable=AsyncMock),
            patch.object(self.bot, "ask_gpt", side_effect=answer),
            patch.object(self.bot, "speak", side_effect=speak),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(self.bot.run_forever(), 2)

        self.assertEqual(received, [q + ",test speech rules" for q in queries])


if __name__ == "__main__":
    unittest.main()
