"""Check streaming delivery, retries, completion and error handling offline."""
import asyncio
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaogpt.tts.edge import AudioHandler, AudioStream, EdgeLiveTTS
from http.server import ThreadingHTTPServer


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.state = AudioStream()
        self.owner = SimpleNamespace(path='/test.mp3', current=self.state)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), AudioHandler)
        self.server.owner = self.owner
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/test.mp3'

    def tearDown(self):
        self.state.close()
        self.server.shutdown()
        self.server.server_close()

    def test_two_clients_receive_full_stream_and_first_bytes_before_finish(self):
        outputs, started = [], threading.Event()
        def receive():
            with urllib.request.urlopen(self.url, timeout=3) as response:
                prefix = response.read(3)
                started.set()
                outputs.append(prefix + response.read())
        clients = [threading.Thread(target=receive) for _ in range(2)]
        for thread in clients:
            thread.start()
        self.state.append(b'abc')
        self.assertTrue(started.wait(2))
        self.assertFalse(self.state.complete)
        self.state.append(b'def' * 4000)
        self.state.finish()
        for thread in clients:
            thread.join(3)
        self.assertEqual(outputs, [b'abc' + b'def' * 4000] * 2)

    def test_range_replay_and_invalid_range(self):
        self.state.append(b'abcdef' * 2000)
        self.state.finish()
        request = urllib.request.Request(self.url, headers={'Range': 'bytes=1-3'})
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b'bcd')
        request = urllib.request.Request(self.url, headers={'Range': 'bytes=8-2'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 416)
        caught.exception.close()

    def test_cached_requests_do_not_extend_the_playback_window(self):
        self.state.append(b'a' * 6000)
        self.state.finish()
        self.state.first_sent = time.monotonic()
        self.state.playback_end = time.monotonic() + 5
        self.state.delivery_complete = True
        original_end = self.state.playback_end
        with urllib.request.urlopen(self.url, timeout=3) as response:
            self.assertEqual(response.read(), b'a' * 6000)
        self.assertEqual(self.state.playback_end, original_end)

    def test_track_repeat_is_blocked_after_delivery_and_playback_window(self):
        self.state.append(b'a' * 6000)
        self.state.finish()
        self.state.first_sent = time.monotonic() - 5
        self.state.playback_end = time.monotonic() - 1
        self.state.delivery_complete = True
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url, timeout=3)
        self.assertEqual(caught.exception.code, 410)
        caught.exception.close()
        self.assertTrue(self.state.ended.is_set())


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mina = SimpleNamespace(play_by_url=AsyncMock(), player_stop=AsyncMock(return_value=True))
        with patch.dict('os.environ', {'XIAOGPT_HOSTNAME': '127.0.0.1', 'XIAOGPT_PORT': '0'}):
            self.tts = EdgeLiveTTS(self.mina, 'device', SimpleNamespace(tts_options={}))

    async def asyncTearDown(self):
        if self.tts.current:
            self.tts.current.close()
        await asyncio.to_thread(self.tts.httpd.shutdown)
        self.tts.httpd.server_close()

    async def test_text_source_error_does_not_hang(self):
        async def source():
            raise RuntimeError('source failed')
            yield 'unreachable'
        with self.assertRaisesRegex(RuntimeError, 'source failed'):
            await asyncio.wait_for(self.tts.synthesize('zh', source()), timeout=2)
        self.mina.play_by_url.assert_not_awaited()




    async def test_multiple_text_chunks_use_one_play_command_and_preserve_audio(self):
        received = []
        texts = ['第一句话。', '后面的内容。', '最后一句话。']
        class Voice:
            def __init__(self, text, *args, **kwargs):
                self.text = text
            async def stream(self):
                yield {'type': 'audio', 'data': self.text.encode() * 300}
        async def source():
            for text in texts:
                yield text
                await asyncio.sleep(0.04)
        async def play(device, url, **kwargs):
            async def receive():
                def read():
                    with urllib.request.urlopen(url, timeout=3) as response:
                        return response.read()
                received.append(await asyncio.to_thread(read))
                self.tts.current.playback_end = time.monotonic() - 1
                self.tts.current.ended.set()
            self.receiver = asyncio.create_task(receive())
            return True
        self.mina.play_by_url.side_effect = play
        fake_mp3 = SimpleNamespace(info=SimpleNamespace(length=8.0))
        with patch('edge_tts.Communicate', Voice), patch('xiaogpt.tts.edge.MP3', return_value=fake_mp3):
            await asyncio.wait_for(self.tts.synthesize('zh', source()), timeout=3)
            await self.receiver
        self.assertEqual(received, [b''.join(text.encode() * 300 for text in texts)])
        self.mina.play_by_url.assert_awaited_once()
        self.mina.player_stop.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
