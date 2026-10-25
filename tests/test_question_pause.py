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

        self.bot.mina_service.player_pause.side_effect = pause
        self.bot.chatbot = SimpleNamespace(has_history=lambda: True, name="ChatGPT")
        with (
            patch.object(self.bot, "init_all_data", new_callable=AsyncMock),
            patch.object(self.bot, "poll_latest_ask", side_effect=poll),
            patch.object(self.bot, "do_tts", side_effect=prompt),
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
        self.bot.chatbot = SimpleNamespace(
            has_history=lambda: bool(received), name="ChatGPT"
        )

        async def poll():
            for query in queries:
                await self.bot.last_record.put(self.record(query))

        async def answer(query):
            received.append(query)
            yield "test answer"

        async def speak(stream):
            async for _ in stream:
                pass
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
