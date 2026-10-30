"""Interrupt both model generation and the remaining speaker playback."""

import asyncio
import contextlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT


class InterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MiGPT(Config(
            openai_key="test", keyword=["请问"], mute_xiaoai=True, stream=True,
        ))
        self.bot.device_id = "speaker"
        self.bot.mina_service = SimpleNamespace(
            player_pause=AsyncMock(return_value=True),
            player_get_status=AsyncMock(),
        )
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bot.close()
        for handler in self.bot.log.handlers[:]:
            self.bot.log.removeHandler(handler)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def publish(self, query):
        record = {"time": self.bot.last_timestamp + 1, "query": query}
        await self.bot._get_last_query({"data": json.dumps({"records": [record]})})
        return record

    async def test_polling_catches_new_question_after_text_ends_before_audio_ends(self):
        events, polls = [], []
        playing, second_answer, stop_started, allow_stop = (
            asyncio.Event() for _ in range(4)
        )
        current = {"time": self.bot.last_timestamp + 1, "query": "请问第一题"}

        async def fetch(session):
            polls.append(time.monotonic())
            return await self.bot._get_last_query(
                {"data": json.dumps({"records": [current]})}
            )

        async def answer(query):
            yield query

        async def speak(stream):
            text = "".join([part async for part in stream])
            if "第一题" in text:
                playing.set()  # Text is exhausted, but audio is still playing.
                try:
                    await asyncio.Future()
                finally:
                    events.append("stop-start")
                    stop_started.set()
                    await allow_stop.wait()
                    events.append("stop-done")
            else:
                events.append("second-answer")
                second_answer.set()

        async def prompt(text):
            events.append("prompt")

        async def pause(device):
            events.append("pause")
            return True

        self.bot.mina_service.player_pause.side_effect = pause
        with (
            patch.object(self.bot, "init_all_data", new_callable=AsyncMock),
            patch.object(self.bot, "get_latest_ask_from_xiaoai", side_effect=fetch),
            patch.object(self.bot, "ask_gpt", side_effect=answer),
            patch.object(self.bot, "do_tts", side_effect=prompt),
            patch.object(self.bot, "speak", side_effect=speak),
        ):
            runner = self.start(self.bot.run_forever())
            await asyncio.wait_for(playing.wait(), 2)
            current = {"time": current["time"] + 1, "query": "请问第二题"}
            try:
                await asyncio.wait_for(stop_started.wait(), 2.5)
                self.assertFalse(second_answer.is_set())
                self.assertEqual(events.count("prompt"), 1)
            finally:
                allow_stop.set()
            await asyncio.wait_for(second_answer.wait(), 2)
            self.assertGreaterEqual(polls[1] - polls[0], 0.95)
            self.assertEqual(events, [
                "pause", "prompt", "pause", "stop-start", "stop-done",
                "prompt", "second-answer",
            ])
            self.bot.mina_service.player_get_status.assert_not_awaited()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def test_new_question_cancels_model_before_its_first_token(self):
        started, closed = asyncio.Event(), asyncio.Event()

        async def model(query, **kwargs):
            started.set()
            try:
                await asyncio.Future()
                yield "unreachable"
            finally:
                closed.set()

        async def speak(stream):
            async for _ in stream:
                pass

        self.bot.chatbot = SimpleNamespace(ask_stream=model)
        with patch.object(self.bot, "speak", side_effect=speak):
            task = self.start(self.bot._speak_until_interrupted("old"))
            await asyncio.wait_for(started.wait(), 1)
            new = await self.publish("请问换一个问题")
            self.assertEqual(await asyncio.wait_for(task, 1), new)
            self.assertTrue(closed.is_set())

    async def test_new_question_cancels_prompt_and_prefetched_model_together(self):
        prompt_started, model_started, prompt_closed, model_closed = (
            asyncio.Event() for _ in range(4)
        )

        async def prompt():
            prompt_started.set()
            try:
                await asyncio.Future()
            finally:
                prompt_closed.set()

        async def model(query):
            model_started.set()
            try:
                await asyncio.Future()
                yield "unreachable"
            finally:
                model_closed.set()

        with (
            patch.object(self.bot, "_play_waiting_prompt", side_effect=prompt),
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", new_callable=AsyncMock) as speak,
        ):
            task = self.start(self.bot._speak_until_interrupted("old", with_prompt=True))
            await asyncio.wait_for(prompt_started.wait(), 1)
            await asyncio.wait_for(model_started.wait(), 1)
            new = await self.publish("请问第二题")
            self.assertEqual(await asyncio.wait_for(task, 1), new)
            self.assertTrue(prompt_closed.is_set())
            self.assertTrue(model_closed.is_set())
            speak.assert_not_awaited()

    async def test_prefetch_error_propagates_after_prompt_without_hanging(self):
        prompt_started, finish_prompt = asyncio.Event(), asyncio.Event()

        async def prompt():
            prompt_started.set()
            await finish_prompt.wait()

        async def model(query):
            raise RuntimeError("model request failed")
            yield "unreachable"

        async def speak(stream):
            async for _ in stream:
                pass

        with (
            patch.object(self.bot, "_play_waiting_prompt", side_effect=prompt),
            patch.object(self.bot, "ask_gpt", side_effect=model),
            patch.object(self.bot, "speak", side_effect=speak),
        ):
            task = self.start(self.bot._speak_until_interrupted("old", with_prompt=True))
            await asyncio.wait_for(prompt_started.wait(), 1)
            self.assertFalse(task.done())
            finish_prompt.set()
            with self.assertRaisesRegex(RuntimeError, "model request failed"):
                await asyncio.wait_for(task, 1)

    async def test_interruption_closes_model_when_text_consumer_is_suspended(self):
        consumed, model_closed = asyncio.Event(), asyncio.Event()

        async def model(query, **kwargs):
            try:
                yield "first sentence"
                await asyncio.Future()
            finally:
                model_closed.set()

        async def speak(stream):
            await anext(stream)
            consumed.set()
            await asyncio.Future()  # TTS is busy; the generator is at a yield.

        self.bot.chatbot = SimpleNamespace(ask_stream=model)
        with patch.object(self.bot, "speak", side_effect=speak):
            task = self.start(self.bot._speak_until_interrupted("old"))
            await asyncio.wait_for(consumed.wait(), 1)
            new = await self.publish("请问第二题")
            self.assertEqual(await asyncio.wait_for(task, 1), new)
            self.assertTrue(model_closed.is_set())

    async def test_native_command_does_not_interrupt_or_become_an_ai_question(self):
        playing, finish = asyncio.Event(), asyncio.Event()

        async def speak(stream):
            playing.set()
            await finish.wait()

        with patch.object(self.bot, "speak", side_effect=speak):
            task = self.start(self.bot._speak_until_interrupted("old"))
            await playing.wait()
            await self.publish("音量调小一点")
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.bot.mina_service.player_pause.assert_not_awaited()
            finish.set()
            self.assertIsNone(await asyncio.wait_for(task, 1))

    async def test_continuous_followup_and_end_command_interrupt_playback(self):
        self.bot.in_conversation = True
        for query in ("再详细讲讲", self.bot.config.end_conversation):
            with self.subTest(query=query):
                playing, stopped = asyncio.Event(), asyncio.Event()

                async def speak(stream):
                    playing.set()
                    try:
                        await asyncio.Future()
                    finally:
                        stopped.set()

                with patch.object(self.bot, "speak", side_effect=speak):
                    task = self.start(self.bot._speak_until_interrupted("old"))
                    await playing.wait()
                    new = await self.publish(query)
                    self.assertEqual(await asyncio.wait_for(task, 1), new)
                    self.assertTrue(stopped.is_set())

    async def test_record_arriving_as_playback_finishes_is_not_lost(self):
        new = {"query": "请问第二题"}

        async def speak(stream):
            self.bot.last_record.put_nowait(new)

        with patch.object(self.bot, "speak", side_effect=speak):
            self.assertEqual(await self.bot._speak_until_interrupted("old"), new)

    async def test_stream_failure_propagates_without_leaking_the_collector(self):
        async def model(query, **kwargs):
            raise RuntimeError("provider failure")
            yield "unreachable"

        self.bot.chatbot = SimpleNamespace(ask_stream=model)
        async with contextlib.aclosing(self.bot.ask_gpt("test")) as stream:
            with self.assertRaisesRegex(RuntimeError, "provider failure"):
                await asyncio.wait_for(anext(stream), 1)


if __name__ == "__main__":
    unittest.main()
