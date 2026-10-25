"""Verify the actual API payload across standalone and continuous questions."""

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaogpt.bot.chatgptapi_bot import ChatGPTBot
from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT


class ConversationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, queries, *, stream):
        bot = MiGPT(
            Config(
                account="test",
                password="test",
                openai_key="test",
                hardware="L05C",
                keyword=["请问", "请对比", "补充"],
                prompt="test speech rules",
                mute_xiaoai=True,
                stream=stream,
            )
        )
        payloads = []
        poll_tasks = []
        processed = None

        async def create(**kwargs):
            payloads.append(copy.deepcopy(kwargs))
            answer = f"answer-{len(payloads)}。"
            if kwargs.get("stream"):
                async def events():
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=answer))]
                    )

                return events()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=answer))]
            )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        async def poll():
            poll_tasks.append(asyncio.current_task())
            await asyncio.Future()

        async def acknowledge():
            if processed is not None and not processed.done():
                processed.set_result(None)

        async def speak(text_stream):
            async for _ in text_stream:
                pass
            await acknowledge()

        with (
            patch.object(ChatGPTBot, "_make_openai_client", return_value=client),
            patch.object(bot, "init_all_data", new_callable=AsyncMock),
            patch.object(bot, "poll_latest_ask", side_effect=poll),
            patch.object(bot, "do_tts", new_callable=AsyncMock),
            patch.object(bot, "wakeup_xiaoai", new_callable=AsyncMock),
            patch.object(bot, "stop_if_xiaoai_is_playing", side_effect=acknowledge),
            patch.object(bot, "speak", side_effect=speak),
        ):
            runner = asyncio.create_task(bot.run_forever())
            try:
                for query in queries:
                    processed = asyncio.get_running_loop().create_future()
                    await bot.last_record.put({"query": query})
                    await asyncio.wait_for(processed, 3)
            finally:
                runner.cancel()
                for task in poll_tasks:
                    task.cancel()
                await asyncio.gather(runner, *poll_tasks, return_exceptions=True)
                await bot.close()
        return [payload["messages"] for payload in payloads]

    async def test_standalone_requests_only_send_current_query_and_prompt(self):
        queries = ["请问官网怎么找", "请对比两款汽车", "补充，我在乎价格"]
        for stream in (False, True):
            with self.subTest(stream=stream):
                messages = await self.exercise(queries, stream=stream)
                self.assertEqual(
                    messages,
                    [[{"role": "user", "content": q + ",test speech rules"}]
                     for q in queries],
                )

    async def test_mode_changes_isolate_sessions_and_keep_followup_context(self):
        queries = [
            "请问旧问题",
            "开始持续对话",
            "介绍红外遥控器",
            "开始持续对话",  # Repeating the command must not erase this session.
            "再详细讲讲",
            "结束持续对话",
            "补充，新问题",
            "开始持续对话",
            "对比两款汽车",
            "结束持续对话",
        ]
        for stream in (False, True):
            with self.subTest(stream=stream):
                messages = await self.exercise(queries, stream=stream)
                self.assertEqual([len(m) for m in messages], [1, 1, 3, 1, 1])
                self.assertEqual(messages[1][0]["content"], "介绍红外遥控器,test speech rules")
                self.assertEqual(messages[2], messages[1] + [
                    {"role": "assistant", "content": "answer-2。"},
                    {"role": "user", "content": "再详细讲讲"},
                ])
                self.assertEqual(messages[3][0]["content"], "补充，新问题,test speech rules")
                self.assertEqual(messages[4][0]["content"], "对比两款汽车,test speech rules")

    async def test_continuous_history_still_uses_existing_window_beyond_five_rounds(self):
        queries = ["开始持续对话"] + [f"问题{i}" for i in range(8)]
        messages = await self.exercise(queries, stream=True)
        self.assertEqual([len(m) for m in messages], [1, 3, 5, 7, 9, 11, 13, 13])
        self.assertEqual(messages[-1][0]["content"], "问题0,test speech rules")
        self.assertEqual(messages[-1][2]["content"], "问题2")
        self.assertEqual(messages[-1][-1]["content"], "问题7")


if __name__ == "__main__":
    unittest.main()
