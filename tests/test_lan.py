"""Direct video policy, encrypted packet fixtures and failed-session cleanup."""
import asyncio
import hashlib
import hmac
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Crypto.Cipher import AES
from custom_components.dreame_live_camera import aliyun, lan, media
from custom_components.dreame_live_camera.const import VIDEO_STREAM_TYPE


def packet(seq, timestamp, payload, marker=True):
    return struct.pack("!BBHII", 128, 96 | (128 if marker else 0), seq, timestamp, 123) + payload


class LANTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_uses_the_same_substream_as_lan_start(self):
        identity = aliyun.VideoIdentity(None, "eu")
        identity.post = AsyncMock(return_value={"code":200,"data":{"p2pInfo":{"signalUrl":"wss://video.aliyuncs.com/?session=test"}}})
        await identity.offer("synthetic-channel")
        params = identity.post.call_args.kwargs["payload"]["params"]
        self.assertEqual(params["streamType"], VIDEO_STREAM_TYPE)
        self.assertEqual(VIDEO_STREAM_TYPE, 1)
        self.assertFalse(params["relayEncrypted"])
        receiver = lan.Receiver(None, "10.0.0.9", "wss://video.aliyuncs.com/?session=test", "device")
        self.assertEqual(receiver.client_id, "client-test")
        await receiver.aclose()

    async def test_no_silent_relay_fallback(self):
        identity = aliyun.VideoIdentity(None, "eu")
        identity.post = AsyncMock(return_value={"code":200,"data":{"relayUrl":"rtmp://example.invalid/private"}})
        with self.assertRaisesRegex(RuntimeError, "Direct LAN"):
            await identity.offer("synthetic-channel")

    def test_host_and_signaling_restrictions(self):
        for value in ["example.com", "8.8.8.8", "127.0.0.1", "224.0.0.1", "0.0.0.0", "10.0.0.1/24"]:
            with self.assertRaises(ValueError):
                media.private_ipv4(value)
        self.assertEqual(media.device_address({"gw":"10.0.0.1", "data":{"localIp":"10.0.0.9"}}), "10.0.0.9")
        self.assertIsNone(media.device_address({"gw":"10.0.0.1"}))
        self.assertEqual(lan.signaling_session("wss://video.aliyuncs.com/connect?session=test"), "test")
        for url in ["ws://video.aliyuncs.com/?session=x", "wss://aliyuncs.com.evil.invalid/?session=x", "wss://user:pass@video.aliyuncs.com/?session=x", "wss://video.aliyuncs.com/", "wss://video.aliyuncs.com/?session=a&session=b"]:
            with self.assertRaises(ValueError):
                lan.signaling_session(url)

    def test_public_relay_and_other_host_candidates_rejected(self):
        host = "10.0.0.9"
        self.assertIsNotNone(lan.allowed_candidate("a 1 udp 100 10.0.0.9 1234 typ host", host))
        for value in ["a 1 udp 100 8.8.8.8 1234 typ host", "a 1 udp 100 10.0.0.8 1234 typ host", "a 1 udp 100 10.0.0.9 1234 typ relay", "a 2 udp 100 10.0.0.9 1234 typ host", "a 1 tcp 100 10.0.0.9 1234 typ host"]:
            self.assertIsNone(lan.allowed_candidate(value, host))

    def test_sdp_integrity(self):
        connection = SimpleNamespace(local_username="test", local_password="test-password")
        lines = lan.make_offer(connection, []).split("\r\n")
        group = next(line.split(":", 1)[1] for line in lines if line.startswith("a=ssrc-group:"))
        digest = hmac.new(b"test-password", group.encode(), hashlib.sha1).hexdigest()
        self.assertIn("a=extdata:" + digest + "_0.8.5", lines)

    def test_vendor_header_and_decryption(self):
        password = "test-password-0123456789"
        clear = struct.pack("!BBHII", 0x90, 0xe0, 1, 42, 123) + b"\xbb\xbb\x00\x03" + bytes(12) + b"\x03\x00\x00\x00\x00\x01\x65" + b"a" * 50
        end = 16 + ((len(clear) - 16) // 16) * 16
        encrypted = clear[:16] + AES.new(password.encode()[:16], AES.MODE_CBC, iv=bytes(16)).encrypt(clear[16:end]) + clear[end:]
        restored = media.decrypt_rtp(encrypted, password)
        self.assertEqual(restored, clear)
        self.assertEqual(media.VideoFrames().feed(restored), b"\x00\x00\x00\x01\x65" + b"a" * 50)

    def test_packet_loss_and_sequence_wrap(self):
        frames = media.VideoFrames()
        self.assertIsNone(frames.feed(packet(65535, 1, b"\x03\x00abc", False)))
        self.assertEqual(frames.feed(packet(0, 1, b"\x01\x00def")), b"abcdef")
        frames.feed(packet(1, 2, b"\x02\x00abc", False))
        self.assertIsNone(frames.feed(packet(3, 2, b"\x00\x00def")))
        self.assertEqual(frames.dropped, 1)
        self.assertEqual(frames.feed(packet(4, 3, b"\x02\x00new")), b"new")

    def test_malformed_packet_and_frame_bounds(self):
        frames = media.VideoFrames()
        for value in [b"", b"\x80", packet(1, 1, b"\x00\x00unjoined"), packet(1, 1, b"\x06\x18dummy")]:
            self.assertIsNone(frames.feed(value))
        self.assertIsNone(frames.feed(packet(1, 2, b"\x02\x00" + b"x" * (2 * 1024 * 1024 + 1))))
        self.assertLessEqual(len(frames.buffer), 2 * 1024 * 1024)

    async def test_failed_handshake_closes_transport_and_is_idempotent(self):
        http = SimpleNamespace(ws_connect=AsyncMock(side_effect=OSError("private URL must not be logged")))
        receiver = lan.Receiver(http, "10.0.0.9", "wss://video.aliyuncs.com/?session=private", "private-device")
        connection = receiver.connection
        candidate = SimpleNamespace(host="10.0.0.2")
        connection._local_candidates = [candidate]
        connection._protocols = []
        with patch.object(connection, "gather_candidates", new=AsyncMock()), patch.object(lan, "route_address", return_value="10.0.0.2"), patch.object(connection, "close", new=AsyncMock()) as close:
            with self.assertRaises(OSError):
                await receiver.run(AsyncMock())
            await receiver.aclose()
            close.assert_awaited_once()
        self.assertTrue(receiver.closed)
        self.assertIsNone(receiver.signal_url)
        self.assertIsNone(receiver.session)

    async def test_unused_interface_does_not_signal_media_eof(self):
        http = SimpleNamespace(ws_connect=AsyncMock())
        receiver = lan.Receiver(http, "10.0.0.9", "wss://video.aliyuncs.com/?session=test", "device")
        connection = receiver.connection
        selected = SimpleNamespace(local_candidate=SimpleNamespace(host="10.0.0.2"), datagram_received=lambda *args:None, close=AsyncMock())
        async def close_unused():
            # This is aioice's real connection_lost behavior: one socket signals EOF.
            connection.data_received(None, None)
        unused = SimpleNamespace(local_candidate=SimpleNamespace(host="172.17.0.1"), close=AsyncMock(side_effect=close_unused))
        connection._protocols = [selected, unused]
        connection._local_candidates = [SimpleNamespace(host="10.0.0.2"), SimpleNamespace(host="172.17.0.1")]
        async def websocket(*args, **kwargs):
            self.assertTrue(connection._queue.empty(), "Discarding an interface must not end the live stream")
            self.assertEqual(connection._protocols, [selected])
            raise OSError("End synthetic negotiation")
        http.ws_connect.side_effect = websocket
        with patch.object(connection, "gather_candidates", new=AsyncMock()), patch.object(lan, "route_address", return_value="10.0.0.2"), patch.object(connection, "close", new=AsyncMock()):
            with self.assertRaises(OSError):
                await receiver.run(AsyncMock())
        unused.close.assert_awaited_once()

    async def test_direct_ice_pair_and_datagram_delivery(self):
        # Real UDP sockets on the current host's LAN interface; no external server.
        host = await asyncio.to_thread(lan.route_address, "10.42.0.180")
        a = lan.LANConnection(host)
        b = lan.aioice.Connection(ice_controlling=False, components=1, use_ipv6=False)
        try:
            await asyncio.gather(a.gather_candidates(), b.gather_candidates())
            a.remote_username, a.remote_password = b.local_username, b.local_password
            b.remote_username, b.remote_password = a.local_username, a.local_password
            for c in b.local_candidates:
                if c.host == host: await a.add_remote_candidate(c)
            for c in a.local_candidates:
                if c.host == host: await b.add_remote_candidate(c)
            await a.add_remote_candidate(None)
            await b.add_remote_candidate(None)
            await asyncio.wait_for(asyncio.gather(a.connect(), b.connect()), 3)
            await b.send(b"synthetic-camera-packet")
            self.assertEqual(await asyncio.wait_for(a.recv(), 1), b"synthetic-camera-packet")
            self.assertEqual(a._nominated[1].remote_candidate.host, host)
        finally:
            await asyncio.gather(a.close(), b.close())


if __name__ == "__main__":
    unittest.main()
