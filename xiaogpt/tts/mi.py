import logging
import re
from typing import AsyncIterator, Iterator

from miservice import MiIOService, MiNAService, miio_command

from xiaogpt.config import Config
from xiaogpt.tts.base import TTS
from xiaogpt.utils import calculate_tts_elapse


logger = logging.getLogger(__name__)
MAX_TTS_CHARS = 200
# Command acceptance does not indicate when audio starts or finishes. Retain
# the upstream estimate and reserve startup time before sending the next text.
TTS_MIN_DURATION = 3.0
TTS_STARTUP_GRACE = 1.0
_SENTENCE_END = re.compile(r"[。！？!?；;\r\n]+[”’」』】）》\"']*")
_SOFT_BREAKS = "，,、：: \t"
_TRAILING_PUNCTUATION = "。！？!?；;，,、：:.”’」』】）》\"'"


def split_tts_text(text: str, max_chars: int = MAX_TTS_CHARS) -> Iterator[str]:
    """Pack adjacent sentences into bounded commands to avoid frequent pauses."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    def split_long_sentence(sentence: str) -> Iterator[str]:
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            window = sentence[:max_chars]
            cut = max(window.rfind(mark) for mark in _SOFT_BREAKS) + 1
            if not cut:
                cut = max_chars
            # Keep trailing punctuation with at least one spoken character.
            while cut > 1 and sentence[cut] in _TRAILING_PUNCTUATION:
                cut -= 1
            part = sentence[:cut].strip()
            if part:
                yield part
            sentence = sentence[cut:].strip()
        if sentence:
            yield sentence

    def sentence_parts() -> Iterator[str]:
        start = 0
        for match in _SENTENCE_END.finditer(text):
            yield from split_long_sentence(text[start : match.end()])
            start = match.end()
        yield from split_long_sentence(text[start:])

    pending = ""
    for part in sentence_parts():
        if pending and len(pending) + len(part) > max_chars:
            yield pending
            pending = ""
        pending += part
    if pending:
        yield pending


class MiTTS(TTS):
    def __init__(
        self, mina_service: MiNAService, device_id: str, config: Config
    ) -> None:
        super().__init__(mina_service, device_id, config)
        self.miio_service = MiIOService(mina_service.account)

    async def say(self, text: str) -> None:
        if not self.config.use_command:
            accepted = await self.mina_service.text_to_speech(self.device_id, text)
            if not accepted:
                raise RuntimeError("Mi TTS request was rejected by the speaker service")
        else:
            result = await miio_command(
                self.miio_service,
                self.config.mi_did,
                f"{self.config.tts_command} {text}",
            )
            if type(result) is not int:
                raise RuntimeError(
                    f"Mi TTS command returned an invalid result ({type(result).__name__})"
                )
            if result != 0:
                raise RuntimeError(f"Mi TTS command was rejected (code={result})")

    async def synthesize(self, lang: str, text_stream: AsyncIterator[str]) -> None:
        chunk_index = 0
        async for text in text_stream:
            for chunk in split_tts_text(text):
                chunk_index += 1
                try:
                    await self.say(chunk)
                except Exception:
                    logger.exception(
                        "Mi TTS chunk %d failed (%d chars)", chunk_index, len(chunk)
                    )
                    raise
                wait_seconds = max(
                    TTS_MIN_DURATION,
                    calculate_tts_elapse(chunk) + TTS_STARTUP_GRACE,
                )
                logger.info(
                    "Mi TTS chunk %d accepted (%d chars, wait %.1fs)",
                    chunk_index,
                    len(chunk),
                    wait_seconds,
                )
                await self.wait_for_duration(wait_seconds)
        logger.info("Mi TTS completed (%d chunks)", chunk_index)
