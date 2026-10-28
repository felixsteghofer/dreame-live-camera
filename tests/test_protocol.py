"""Protocol tests with synthetic media; no robot, account or private fixtures."""
import asyncio
import importlib.util
import pathlib
import struct
import unittest

path = pathlib.Path(__file__).parents[1] / "custom_components/dreame_live_camera/rtmp.py"
if not path.exists():
    path = pathlib.Path("/config/custom_components/dreame_live_camera/rtmp.py")
spec = importlib.util.spec_from_file_location("rtmp", path)
rtmp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rtmp)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_continuations_and_timestamp_delta(self):
        receiver = rtmp.Receiver("rtmp://127.0.0.1/live?token=example/session")
        receiver.chunk_size = 3
        reader = asyncio.StreamReader()
        receiver.reader = reader
        first = rtmp.message(8, 9, b"abcdef", msid=1)
        # Insert a continuation header after the first three payload bytes.
        first = first[:15] + b"\xc8" + first[15:]
        delta = b"\x48\x00\x00\x05\x00\x00\x02\x09gh"
        reader.feed_data(first + delta)
        self.assertEqual(await receiver.read_message(), (9, 0, b"abcdef"))
        self.assertEqual(await receiver.read_message(), (9, 5, b"gh"))
        reader.feed_eof()

    async def test_extended_timestamp_and_size_bound(self):
        receiver = rtmp.Receiver("rtmp://127.0.0.1/live/path")
        receiver.reader = asyncio.StreamReader()
        receiver.reader.feed_data(b"\x08\xff\xff\xff\x00\x00\x01\x09\x01\x00\x00\x00" + struct.pack(">I", 0x1000000) + b"v")
        self.assertEqual(await receiver.read_message(), (9, 0x1000000, b"v"))
        receiver.reader.feed_data(b"\x08\x00\x00\x00\xff\xff\xff\x09\x01\x00\x00\x00")
        with self.assertRaises(ValueError):
            await receiver.read_message()

    async def test_real_tcp_handshake_and_flv_forwarding(self):
        observed = []
        async def relay(reader, writer):
            try:
                opening = await reader.readexactly(1537)
                self.assertEqual(opening[0], 4)
                writer.write(b"\x03" + bytes(1536) + rtmp.message(8, 9, b"\x17\x00\x00\x00\x00synthetic", msid=1))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
        server = await asyncio.start_server(relay, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        receiver = rtmp.Receiver(f"rtmp://127.0.0.1:{port}/live?token=example/key")
        async def output(data):
            observed.append(data)
            if len(observed) == 2:
                receiver.close()
        await receiver.run(output)
        server.close()
        await server.wait_closed()
        self.assertTrue(observed[0].startswith(b"FLV"))
        self.assertEqual(observed[1], rtmp.flv_tag(9, 0, b"\x17\x00\x00\x00\x00synthetic"))
        self.assertIn("cacheDuration=3000", receiver.app)

    def test_relay_urls_reject_credentials_and_non_rtmp(self):
        for value in ["https://example.invalid/stream", "rtmp://user:secret@example.invalid/a/b"]:
            with self.assertRaises(ValueError):
                rtmp.Receiver(value)

    async def test_async_relay_close_waits_for_socket_and_clears_signed_url(self):
        from unittest.mock import AsyncMock, Mock
        receiver = rtmp.Receiver("rtmp://127.0.0.1/live?token=synthetic/session")
        receiver.writer = type("Writer",(),{"close":Mock(),"wait_closed":AsyncMock()})()
        await receiver.aclose()
        await receiver.aclose()
        self.assertTrue(receiver.cleanup_confirmed)
        self.assertIsNone(receiver.tc_url)
        self.assertIsNone(receiver.playpath)
        receiver.writer.wait_closed.assert_awaited()


if __name__ == "__main__":
    unittest.main()
