"""Free Edge speech delivered as one replay-safe MP3 stream per answer."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mutagen.mp3 import MP3

from xiaogpt.tts.base import TTS, logger
from xiaogpt.tts.mi import split_tts_text
from xiaogpt.utils import get_hostname


class AudioStream:
    def __init__(self):
        self.condition = threading.Condition()
        self.audio = bytearray()
        self.complete = False
        self.closed = False
        self.error = None
        self.first_sent = None
        self.playback_end = 0.0
        self.delivery_complete = False
        self.ended = threading.Event()
        self.duration = 0.0

    def append(self, data):
        with self.condition:
            if len(self.audio) + len(data) > 16 * 1024 * 1024:
                raise ValueError('Edge audio exceeds the 16 MiB answer limit')
            self.audio.extend(data)
            self.condition.notify_all()

    def finish(self, error=None):
        with self.condition:
            self.error = error
            self.complete = True
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class AudioHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def do_HEAD(self):
        if self.path != self.server.owner.path:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Connection', 'close')
        self.end_headers()

    def do_GET(self):
        owner = self.server.owner
        state = owner.current
        if self.path != owner.path or state is None:
            self.send_error(404)
            return
        self.connection.settimeout(10)
        raw_range = self.headers.get('Range', '')
        now = time.monotonic()
        with state.condition:
            if state.closed:
                self.send_error(410)
                return
            # This firmware requests the track again when it reaches its end.
            # Refuse that replay only after all bytes were delivered and the
            # measured MP3 playback window has elapsed. Early retries still work.
            if (state.delivery_complete and state.first_sent is not None
                    and now >= state.playback_end - 0.5
                    and raw_range in ('', 'bytes=0-')):
                state.ended.set()
                logger.info('Edge end-of-track replay detected')
                self.send_error(410)
                return
            finished, size = state.complete, len(state.audio)
        offset, end, status = 0, None, 200
        if finished:
            end = size
            if raw_range:
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', raw_range)
                if not match or int(match[1]) >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                offset = int(match[1])
                end = min(size, int(match[2]) + 1) if match[2] else size
                if end <= offset:
                    self.send_error(416)
                    return
                status = 206
        self.send_response(status)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        if finished:
            self.send_header('Content-Length', str(end - offset))
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {offset}-{end-1}/{size}')
        self.end_headers()
        playback_clock = None
        try:
            while True:
                with state.condition:
                    ready = state.condition.wait_for(
                        lambda: len(state.audio) > offset or state.complete or state.closed,
                        timeout=45,
                    )
                    if not ready or state.closed:
                        break
                    limit = min(len(state.audio), end) if end is not None else len(state.audio)
                    packet = bytes(state.audio[offset:min(offset + 32768, limit)])
                    if not packet and (state.complete or end is not None):
                        if offset == len(state.audio):
                            state.delivery_complete = True
                        break
                self.wfile.write(packet)
                self.wfile.flush()
                now = time.monotonic()
                with state.condition:
                    if state.first_sent is None:
                        state.first_sent = now
                    # Edge returns constant 48 kbps MP3. Account for actual
                    # delivery stalls rather than guessing from text length.
                    if not state.delivery_complete:
                        playback_clock = max(playback_clock or now, now) + len(packet) / 6000
                        state.playback_end = max(state.playback_end, playback_clock)
                offset += len(packet)
        except (OSError, TimeoutError):
            pass

    def log_message(self, *args):
        pass


class EdgeLiveTTS(TTS):
    def __init__(self, mina_service, device_id, config):
        super().__init__(mina_service, device_id, config)
        self.options = dict(config.tts_options)
        self.voice = self.options.pop('voice', 'zh-CN-XiaoxiaoNeural')
        unknown = set(self.options) - {'rate', 'volume', 'pitch', 'proxy'}
        if unknown:
            raise ValueError(f'Unsupported Edge options: {sorted(unknown)}')
        self.hostname = get_hostname()
        self.current = None
        self.path = ''
        self.httpd = ThreadingHTTPServer(
            (self.hostname, int(os.getenv('XIAOGPT_PORT', '0'))), AudioHandler,
        )
        self.httpd.owner = self
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        logger.info('Edge continuous audio ready on %s:%d', self.hostname, self.httpd.server_port)

    async def _produce(self, text_stream, state):
        import edge_tts

        queue = asyncio.Queue(maxsize=8)

        async def collect():
            async with asyncio.timeout(180):
                async for text in text_stream:
                    for part in split_tts_text(text, max_chars=160):
                        await queue.put(part)
            await queue.put(None)

        collector = asyncio.create_task(collect())
        item = None
        try:
            done = False
            while not done:
                # Surface producer errors rather than waiting on an empty queue.
                if collector.done() and collector.exception():
                    raise collector.exception()
                item = asyncio.create_task(queue.get())
                ready, _ = await asyncio.wait(
                    [item, collector], return_when=asyncio.FIRST_COMPLETED,
                )
                if collector in ready and collector.exception():
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
                    raise collector.exception()
                text = await item
                if text is None:
                    break
                # Read ahead without adding a fixed wait between sentences.
                while not queue.empty() and len(text) < 160:
                    part = queue.get_nowait()
                    if part is None:
                        done = True
                        break
                    text += part
                logger.info('Edge synthesizing %d characters', len(text))
                async with asyncio.timeout(60):
                    async for chunk in edge_tts.Communicate(
                        text, self.voice, connect_timeout=10, receive_timeout=20,
                        **self.options,
                    ).stream():
                        if chunk['type'] == 'audio':
                            state.append(chunk['data'])
            if state.audio:
                state.duration = MP3(io.BytesIO(state.audio)).info.length
            state.finish()
            logger.info('Edge audio complete: %.2f seconds, %d bytes', state.duration, len(state.audio))
        except BaseException as exc:
            state.finish(exc)
            raise
        finally:
            if item is not None:
                item.cancel()
                await asyncio.gather(item, return_exceptions=True)
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

    @staticmethod
    def _read_mp3(path):
        path = Path(path)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Cached audio exceeds the 16 MiB limit')
        audio = path.read_bytes()
        duration = MP3(io.BytesIO(audio)).info.length
        if not audio or duration <= 0:
            raise ValueError('Cached audio is empty or invalid')
        return audio, duration

    async def cache_prompt(self, text, directory):
        settings = {
            'text': text,
            'voice': self.voice,
            'options': {k: v for k, v in self.options.items() if k != 'proxy'},
        }
        key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:24]
        path = Path(directory) / f'waiting-prompt-{key}.mp3'
        try:
            await asyncio.to_thread(self._read_mp3, path)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning('Invalid cached waiting prompt; regenerating audio')
        else:
            logger.info('Edge waiting prompt cache hit: %s', path.name)
            return path

        async def source():
            yield text

        state = AudioStream()
        await self._produce(source(), state)
        if not state.audio or state.duration <= 0:
            raise RuntimeError('Edge generated no waiting-prompt audio')
        audio = bytes(state.audio)

        def save():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as file:
                    file.write(audio)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()

        await asyncio.to_thread(save)
        logger.info('Edge waiting prompt cached: %.2f seconds, %s', state.duration, path)
        return path

    async def play_file(self, path):
        audio, duration = await asyncio.to_thread(self._read_mp3, path)
        state = AudioStream()
        state.append(audio)
        state.duration = duration
        state.finish()
        ready = asyncio.get_running_loop().create_future()
        ready.set_result(None)
        await self._play(state, ready)

    async def synthesize(self, lang, text_stream):
        state = AudioStream()
        worker = asyncio.create_task(self._produce(text_stream, state))
        await self._play(state, worker)

    async def _play(self, state, worker):
        self.current = state
        self.path = '/' + uuid.uuid4().hex + '.mp3'
        started = time.monotonic()
        play_request = None
        try:
            async with asyncio.timeout(90):
                while not state.audio and not worker.done():
                    await asyncio.sleep(0.02)
            if worker.done():
                await worker
            if not state.audio:
                return
            url = f'http://{self.hostname}:{self.httpd.server_port}{self.path}'
            play_request = asyncio.create_task(
                self.mina_service.play_by_url(self.device_id, url, _type=1)
            )
            # A canceled HTTP await does not retract a command already sent to
            # Xiaomi. Let it settle before stopping this track in cleanup.
            playing = await asyncio.shield(play_request)
            if not playing:
                raise RuntimeError('Speaker rejected Edge audio URL')
            logger.info('Edge playback accepted after %.2f seconds', time.monotonic() - started)
            while not state.ended.is_set():
                if worker.done():
                    await worker
                now = time.monotonic()
                if state.first_sent is None and now - started > 40:
                    raise TimeoutError('Speaker did not request Edge audio')
                if state.delivery_complete and now > state.playback_end + 10:
                    logger.info('Edge playback finished by audio-duration fallback')
                    break
                await asyncio.sleep(0.1)
            await worker
            # A next-track prefetch can precede the final audible samples.
            # Allow those samples to drain before issuing the stop command.
            await asyncio.sleep(max(0, state.playback_end + 1 - time.monotonic()))
        finally:
            state.close()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if play_request is not None:
                with contextlib.suppress(Exception):
                    await play_request
                with contextlib.suppress(Exception):
                    await self.mina_service.player_stop(self.device_id)
            self.current = None
            logger.info('Edge playback closed')
