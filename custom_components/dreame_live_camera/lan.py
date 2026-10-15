"""Cloud-signaled, direct-LAN LinkVisual video. No media relay fallback.

The aioice private APIs used here are isolated and pinned in manifest.json.
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import socket
import struct
import time
from urllib.parse import parse_qs, urlsplit

import aioice
from aioice import stun
from aiohttp import ClientWSTimeout, WSMsgType

from .const import VIDEO_STREAM_TYPE
from .media import VideoFrames, decrypt_rtp, private_ipv4


def route_address(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((host, 9))
        return route.getsockname()[0]


def signaling_session(url):
    parts = urlsplit(url)
    if (parts.scheme != "wss" or not parts.hostname
        or not parts.hostname.endswith(".aliyuncs.com")
        or parts.username or parts.password or parts.fragment
        or parts.port not in (None, 443)):
        raise ValueError("Invalid camera signaling endpoint")
    sessions = parse_qs(parts.query).get("session", [])
    if len(sessions) != 1 or not 1 <= len(sessions[0]) <= 512:
        raise ValueError("Invalid camera signaling session")
    return sessions[0]


def allowed_candidate(raw, host):
    candidate = aioice.Candidate.from_sdp(raw.removeprefix("candidate:"))
    if (candidate.host == host and candidate.type == "host"
        and candidate.transport.lower() == "udp" and candidate.component == 1
        and 1 <= candidate.port <= 65535):
        return candidate
    return None


def make_offer(connection, candidates):
    ssrc = secrets.randbits(30)
    group = f"FID {ssrc} {ssrc + 1}"
    lines = ["v=0", f"o=- {secrets.randbits(48)} 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
        "a=group:BUNDLE video", "a=msid-semantic: WMS stream", "m=video 9 RTP/AVPF 96 97",
        "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{connection.local_username}", f"a=ice-pwd:{connection.local_password}",
        "a=ice-options:trickle", "a=mid:video", "a=sendrecv", "a=rtcp-mux",
        "a=rtpmap:96 H264/90000", "a=rtcp-fb:96 nack", "a=rtcp-fb:96 nack pli",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=rtpmap:97 rtx/90000", "a=fmtp:97 apt=96", f"a=ssrc-group:{group}",
        f"a=ssrc:{ssrc} cname:ha-camera", f"a=ssrc:{ssrc + 1} cname:ha-camera"]
    lines += ["a=candidate:" + candidate.to_sdp() for candidate in candidates]
    digest = hmac.new(connection.local_password.encode(), group.encode(), hashlib.sha1).hexdigest()
    lines.append("a=extdata:" + digest + "_0.8.5")
    return "\r\n".join(lines) + "\r\n"


class LANConnection(aioice.Connection):
    def __init__(self, host):
        super().__init__(ice_controlling=True, components=1, use_ipv6=False)
        self.host = host

    def request_received(self, message, addr, protocol, raw_data):
        if addr[0] == self.host:
            super().request_received(message, addr, protocol, raw_data)


class Receiver:
    """Own one direct session; all credentials and images stay in memory."""
    def __init__(self, http, host, signal_url, channel_id):
        self.http, self.host = http, private_ipv4(host)
        self.session = signaling_session(signal_url)
        self.signal_url, self.channel_id = signal_url, channel_id
        self.client_id = "client-" + self.session
        self.connection = LANConnection(self.host)
        self.ws = self.signals = None
        self.answer = asyncio.Event()
        self.remote_ssrc = 0
        self.peer = None
        self.datagrams = self.bytes = 0
        self.frames = VideoFrames()
        self.closed = False
        self.cleanup_confirmed = False
        self._close_lock = asyncio.Lock()
        self._seq = 1
        self._unused_protocols = []
        self._candidate_count = 0
        self._seen_candidates = set()

    async def _candidate(self, raw):
        if raw in self._seen_candidates or self._candidate_count >= 32:
            return
        self._seen_candidates.add(raw)
        self._candidate_count += 1
        try:
            candidate = allowed_candidate(raw, self.host)
        except (ValueError, IndexError):
            return
        if candidate:
            await self.connection.add_remote_candidate(candidate)

    async def _send(self, params):
        await self.ws.send_json({"version":"1.0.1", "params":{
            "iotId":self.channel_id, "clientId":self.client_id, "clientType":"pc",
            "channelType":"websocket", "sessionId":self.session, **params}})

    async def _signals(self):
        try:
            async for message in self.ws:
                if message.type != WSMsgType.TEXT:
                    continue
                value = json.loads(message.data)
                params = value.get("params", {})
                if params.get("sessionId") != self.session:
                    continue
                if params.get("messageType") == "answerSdp" and not self.answer.is_set():
                    sdp = params.get("sdp", "")
                    if not isinstance(sdp, str) or len(sdp) > 65536:
                        raise ValueError("Invalid camera answer")
                    for line in sdp.replace("\r", "").split("\n"):
                        if line.startswith("a=ice-ufrag:"):
                            self.connection.remote_username = line.split(":", 1)[1]
                        elif line.startswith("a=ice-pwd:"):
                            self.connection.remote_password = line.split(":", 1)[1]
                        elif line.startswith("a=candidate:"):
                            await self._candidate(line[12:])
                        elif line.startswith("a=ssrc:") and not self.remote_ssrc:
                            self.remote_ssrc = int(line[7:].split()[0])
                    self.answer.set()
                elif params.get("messageType") == "candidate":
                    raw = params.get("candidate")
                    if isinstance(raw, str) and len(raw) < 2048:
                        await self._candidate(raw)
        finally:
            # Signaling may end once the direct path exists. UDP is independent.
            self.answer.set()

    async def _control(self, kind, *, stream_type=None):
        pair = self.connection._nominated.get(1)
        if not pair or pair.remote_candidate.host != self.host:
            raise RuntimeError("No direct camera connection")
        self._seq += 1
        params = {"messageType":kind, "sessionId":self.session}
        if stream_type is not None:
            params["streamType"] = stream_type
        request = self.connection.build_request(pair, nominate=False)
        request.attributes["SOFTWARE"] = json.dumps({"version":"2.0", "type":"request",
            "seq_num":self._seq, "params":params}, separators=(",", ":"))
        await asyncio.wait_for(pair.protocol.request(request, pair.remote_addr,
            integrity_key=self.connection.remote_password.encode(), retransmissions=1), 2)

    async def _keyframe(self):
        pair = self.connection._nominated[1]
        if self.remote_ssrc:
            await self.connection.send(struct.pack("!BBHII", 0x81, 206, 2, 1, self.remote_ssrc))
            await self.connection.send(struct.pack("!BBHI", 0x80, 201, 1, 1))
        request = self.connection.build_request(pair, nominate=False)
        request.attributes["SOFTWARE"] = "realplay:forceIFrame"
        try:
            await asyncio.wait_for(pair.protocol.request(request, pair.remote_addr,
                integrity_key=self.connection.remote_password.encode(), retransmissions=0), 1)
        except (TimeoutError, stun.TransactionError):
            pass

    async def run(self, output):
        try:
            await self.connection.gather_candidates()
            address = await asyncio.to_thread(route_address, self.host)
            protocols = []
            for protocol in self.connection._protocols:
                if protocol.local_candidate.host != address:
                    # aioice treats ANY protocol closure as end-of-media. Keep
                    # unused interface sockets alive until the receiver stops,
                    # but exclude them from candidate checks and advertisements.
                    self._unused_protocols.append(protocol)
                    continue
                original = protocol.datagram_received
                def receive(data, addr, original=original):
                    if addr[0] == self.host:
                        original(data, addr)
                protocol.datagram_received = receive
                protocols.append(protocol)
            self.connection._protocols = protocols
            candidates = [c for c in self.connection.local_candidates if c.host == address]
            self.connection._local_candidates = candidates
            if not candidates:
                raise RuntimeError("No LAN route to camera")
            async with asyncio.timeout(8):
                self.ws = await self.http.ws_connect(self.signal_url, heartbeat=15,
                    max_msg_size=1024 * 1024, timeout=ClientWSTimeout(ws_close=2))
            self.signals = asyncio.create_task(self._signals())
            await self._send({"messageType":"offerSdp", "Version":"1.3.2",
                "sdp":make_offer(self.connection, candidates)})
            for candidate in candidates:
                await self._send({"messageType":"candidate", "sdpMid":"video",
                    "sdpMLineIndex":0, "candidate":"candidate:" + candidate.to_sdp()})
            await asyncio.wait_for(self.answer.wait(), 12)
            if not self.connection.remote_username or not self.connection.remote_password:
                raise RuntimeError("Camera negotiation failed")
            await asyncio.wait_for(self.connection.connect(), 12)
            pair = self.connection._nominated[1]
            if pair.remote_candidate.host != self.host:
                raise RuntimeError("Camera did not select the LAN path")
            self.peer = self.host
            await self._control("startStreaming", stream_type=VIDEO_STREAM_TYPE)
            await self._keyframe()
            last_keyframe, dropped = time.monotonic(), 0
            while not self.closed:
                packet = await asyncio.wait_for(self.connection.recv(), 10)
                self.datagrams += 1
                self.bytes += len(packet)
                try:
                    frame = self.frames.feed(decrypt_rtp(packet, self.connection.remote_password))
                except (ValueError, struct.error, IndexError):
                    continue
                if frame:
                    await output(frame)
                if self.frames.dropped > dropped and time.monotonic() - last_keyframe > 2:
                    await self._keyframe()
                    dropped, last_keyframe = self.frames.dropped, time.monotonic()
        finally:
            await self.aclose()

    async def aclose(self):
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            try:
                await self._control("stopStreaming")
                self.cleanup_confirmed = True
            except (Exception, asyncio.CancelledError):
                pass
            if self.signals:
                self.signals.cancel()
                await asyncio.gather(self.signals, return_exceptions=True)
            if self.ws:
                await self.ws.close()
            await self.connection.close()
            await asyncio.gather(*(p.close() for p in self._unused_protocols), return_exceptions=True)
            self._unused_protocols.clear()
            self.signal_url = self.session = None
            self.connection.remote_password = None
