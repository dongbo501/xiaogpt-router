"""Exercise link filtering at arbitrary stream boundaries and at the TTS edge."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT, filter_speech_links, strip_speech_links


async def source(chunks):
    for chunk in chunks:
        yield chunk


async def filtered(chunks):
    return "".join([text async for text in filter_speech_links(source(chunks))])


class SpeechLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_provider_citations_at_every_split(self):
        text = (
            "请搜索国家税务总局官网。"
            "([chinatax.gov.cn](https://www.chinatax.gov.cn/?utm_source=openai))\n"
            "也可以参考能源部资料。"
            "([energy.gov](https://www.energy.gov/cmei/femp/purchasing?utm_source=openai))\n"
            "回答完了。"
        )
        expected = "请搜索国家税务总局官网。也可以参考能源部资料。回答完了。"
        for cut in range(len(text) + 1):
            with self.subTest(cut=cut):
                self.assertEqual((await filtered([text[:cut], text[cut:]])).strip(), expected)
        self.assertEqual(await filtered(list(text)), expected)

    async def test_readable_link_labels_are_kept(self):
        text = "请查看[国家税务总局官网](https://example.com/a_(b)?q=1)，再搜索政策名称。"
        self.assertEqual(await filtered(list(text)), "请查看国家税务总局官网，再搜索政策名称。")

    async def test_query_punctuation_does_not_release_url_fragments(self):
        chunks = ["参考资料：https://example.com/search?", "q=hello;", "page=2!", "#result。回答完了。"]
        self.assertEqual(await filtered(chunks), "参考资料：。回答完了。")

    async def test_bare_domains_and_unicode_paths(self):
        text = "可访问example.com，也可访问dl.lxf.sh/中文资料?版本=新；再搜索官网。"
        self.assertEqual(await filtered(list(text)), "可访问，也可访问；再搜索官网。")

    async def test_nested_url_parentheses_and_surrounding_prose(self):
        text = "参考(https://en.wikipedia.org/wiki/Function_(mathematics))后的说明。"
        self.assertEqual(await filtered(list(text)), "参考后的说明。")

    async def test_plain_text_versions_numbers_and_filenames_are_preserved(self):
        text = "Python 3.14、GPT-4.1 和 Node.js 都是名称。读取 report.pdf，价格3.5元（不含运费）。回答完了。"
        self.assertEqual(await filtered(list(text)), text)

    async def test_images_numeric_and_private_citation_markers(self):
        text = "正文。![图片](https://example.com/a.png)[1][2,3]\ue200cite\ue202turn0search0\ue201\n回答完了。"
        self.assertEqual(await filtered(list(text)), "正文。回答完了。")

    async def test_incomplete_link_at_eof_does_not_leak(self):
        text = "正文。([example.com](https://example.com/a?x="
        self.assertEqual(await filtered(list(text)), "正文。")

    async def test_first_sentence_is_available_before_later_chunks(self):
        release = asyncio.Event()

        async def delayed():
            yield "第一句话已经可以播报。"
            await release.wait()
            yield "来源([example.com](https://example.com))。回答完了。"

        output = filter_speech_links(delayed())
        try:
            first = await asyncio.wait_for(anext(output), 0.5)
            self.assertEqual(first, "第一句话已经可以播报。")
            self.assertFalse(release.is_set())
            release.set()
            self.assertEqual("".join([part async for part in output]), "来源。回答完了。")
        finally:
            release.set()
            await output.aclose()

    async def test_source_error_is_propagated_without_leaking_partial_link(self):
        async def broken():
            yield "([example.com](https://example.com/?"
            raise RuntimeError("stream failed")

        output = filter_speech_links(broken())
        with self.assertRaisesRegex(RuntimeError, "stream failed"):
            await anext(output)

    async def test_tts_receives_filtered_text_and_link_only_reply_is_skipped(self):
        bot = MiGPT(Config(openai_key="test"))
        received = []

        async def synthesize(lang, chunks):
            received.append("".join([chunk async for chunk in chunks]))

        bot.tts = SimpleNamespace(synthesize=AsyncMock(side_effect=synthesize))
        try:
            with patch("xiaogpt.xiaogpt.detect_language", return_value="zh"):
                await bot.speak(source(["请搜索官网。([example.com](https://example.com/?q=1))\n回答完了。"]))
                await bot.speak(source(["https://example.com/?q=1"]))
            self.assertEqual(received, ["请搜索官网。回答完了。"])
            bot.tts.synthesize.assert_awaited_once()
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
