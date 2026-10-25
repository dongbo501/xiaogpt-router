"""Offline regression tests for long replies sent to the original Mi voice."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaogpt.tts.mi import MAX_TTS_CHARS, MiTTS, split_tts_text


class SplitTextTests(unittest.TestCase):
    def test_sentences_and_closing_quotes(self):
        text = "第一句。第二句！第三句？他说：“可以。”最后一句；\n末尾无标点"
        self.assertEqual(
            list(split_tts_text(text, max_chars=9)),
            ["第一句。第二句！", "第三句？", "他说：“可以。”", "最后一句；", "末尾无标点"],
        )

    def test_short_sentences_and_commas_do_not_force_separate_commands(self):
        text = "先按下按钮，芯片生成编码。红外灯闪烁，发送信号。电器收到后执行操作。"
        self.assertLessEqual(len(text), MAX_TTS_CHARS)
        self.assertEqual(list(split_tts_text(text)), [text])

    def test_packing_reduces_command_count_without_exceeding_limit(self):
        text = "这是一个短句。" * 40
        parts = list(split_tts_text(text))
        self.assertEqual("".join(parts), text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(0 < len(part) <= MAX_TTS_CHARS for part in parts))
        self.assertTrue(all(part.endswith("。") for part in parts))

    def test_long_reply_preserves_text_and_bounds_every_chunk(self):
        text = "原理如下。" + ("红外光传送编码，" * 60) + "回答完了。"
        parts = list(split_tts_text(text))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(part) <= MAX_TTS_CHARS for part in parts))
        self.assertEqual("".join(parts), text)
        self.assertEqual(parts[0], "原理如下。")
        self.assertTrue(parts[-1].endswith("回答完了。"))

    def test_long_sentence_prefers_comma(self):
        text = "甲" * 60 + "，" + "乙" * 60 + "。"
        self.assertEqual(list(split_tts_text(text, max_chars=100)), ["甲" * 60 + "，", "乙" * 60 + "。"])

    def test_no_punctuation_and_boundary_punctuation(self):
        for size in (199, 200, 201, 400, 601):
            for ending in ("", "。", "。”"):
                text = "甲" * size + ending
                with self.subTest(size=size, ending=ending):
                    parts = list(split_tts_text(text))
                    self.assertEqual("".join(parts), text)
                    self.assertTrue(all(0 < len(part) <= MAX_TTS_CHARS for part in parts))
                    self.assertTrue(all("甲" in part for part in parts))

    def test_empty_text_and_invalid_limit(self):
        self.assertEqual(list(split_tts_text(" \n\t")), [])
        with self.assertRaises(ValueError):
            list(split_tts_text("文字", 0))


class MiTTSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # No account, credentials, network access or real speaker is used.
        self.tts = object.__new__(MiTTS)
        self.tts.config = SimpleNamespace(
            use_command=True, mi_did="123", tts_command="5-3"
        )
        self.tts.device_id = "test-device"
        self.tts.miio_service = object()
        self.tts.mina_service = SimpleNamespace(text_to_speech=AsyncMock(return_value=True))

    async def test_first_sentence_then_large_buffer_is_played_sequentially(self):
        first = "第一句话。"
        remainder = "后续内容需要完整播放，" * 50 + "回答完了。"
        events = []

        async def stream():
            yield first
            yield remainder

        async def command(_service, _did, value):
            events.append(("say", value.split(" ", 1)[1]))
            return 0

        async def wait(duration):
            self.assertGreater(duration, 1)
            events.append(("wait", duration))

        self.tts.wait_for_duration = wait
        with patch("xiaogpt.tts.mi.miio_command", side_effect=command):
            await self.tts.synthesize("zh", stream())
        spoken = [value for kind, value in events if kind == "say"]
        self.assertEqual("".join(spoken), first + remainder)
        self.assertTrue(all(len(part) <= MAX_TTS_CHARS for part in spoken))
        self.assertEqual([kind for kind, _ in events], ["say", "wait"] * len(spoken))
        self.assertTrue(spoken[-1].endswith("回答完了。"))

    async def test_rejected_command_stops_without_waiting_or_sending_next_sentence(self):
        async def stream():
            yield "第一句。第二句。" * 30

        self.tts.wait_for_duration = AsyncMock()
        with (
            patch("xiaogpt.tts.mi.miio_command", new=AsyncMock(return_value=-4003)) as command,
            self.assertLogs("xiaogpt.tts.mi", level="ERROR"),
            self.assertRaisesRegex(RuntimeError, "code=-4003"),
        ):
            await self.tts.synthesize("zh", stream())
        command.assert_awaited_once()
        self.tts.wait_for_duration.assert_not_awaited()

    async def test_complete_200_character_answer_uses_one_speaker_command(self):
        text = "这是完整的一句话。" * 22 + "好。"
        self.assertEqual(len(text), 200)

        async def stream():
            yield text

        self.tts.wait_for_duration = AsyncMock()
        with patch("xiaogpt.tts.mi.miio_command", new=AsyncMock(return_value=0)) as command:
            await self.tts.synthesize("zh", stream())
        command.assert_awaited_once()
        self.assertEqual(command.await_args.args[2], "5-3 " + text)
        self.tts.wait_for_duration.assert_awaited_once()

    async def test_invalid_command_results_are_not_treated_as_success(self):
        for result in (None, False, True, "0"):
            with self.subTest(result=result):
                with patch("xiaogpt.tts.mi.miio_command", new=AsyncMock(return_value=result)):
                    with self.assertRaises(RuntimeError):
                        await self.tts.say("测试。")

    async def test_general_wait_covers_short_and_long_clipping_regressions(self):
        async def stream():
            yield "开始测试。"
            yield "丙" * 60
            yield "甲" * 85
            yield "乙" * 99

        self.tts.wait_for_duration = AsyncMock()
        with patch("xiaogpt.tts.mi.miio_command", new=AsyncMock(return_value=0)):
            await self.tts.synthesize("zh", stream())
        waits = [call.args[0] for call in self.tts.wait_for_duration.await_args_list]
        # A new 60-character reply clipped at 12 seconds even though the
        # earlier 85-character sample was complete at 17 seconds. Do not tune
        # the whole voice solely to that faster sample or guard only long text.
        self.assertEqual(len(waits), 4)
        self.assertGreaterEqual(waits[0], 3.0)
        self.assertGreater(waits[1], 14.0)
        self.assertGreater(waits[2], 19.5)
        self.assertGreaterEqual(waits[3], 23.0)

    async def test_non_command_rejection_and_network_error_are_not_suppressed(self):
        self.tts.config.use_command = False
        self.tts.mina_service.text_to_speech.return_value = False
        with self.assertRaises(RuntimeError):
            await self.tts.say("测试。")
        self.tts.mina_service.text_to_speech.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.tts.say("测试。")

    async def test_cancellation_does_not_send_next_chunk(self):
        async def stream():
            yield "第一句。第二句。" * 30

        self.tts.wait_for_duration = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("xiaogpt.tts.mi.miio_command", new=AsyncMock(return_value=0)) as command:
            with self.assertRaises(asyncio.CancelledError):
                await self.tts.synthesize("zh", stream())
        command.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
