"""Bounded, async LinkVisual private-mode RTMP receiver.

Protocol ported from @apocaliss92/nodedreame 1.12.5 (MIT). See NOTICE.
Only downlink FLV video is forwarded. No microphone or robot controls.
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
from urllib.parse import urlsplit

LIMIT = 8 * 1024 * 1024


def amf(value):
    if value is None:
        return b"\x05"
    if isinstance(value, bool):
        return b"\x01" + bytes([value])
    if isinstance(value, (float, int)):
        return b"\x00" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode()
        return b"\x02" + len(raw).to_bytes(2, "big") + raw
    if isinstance(value, dict):
        return b"\x03" + b"".join(len(k.encode()).to_bytes(2, "big") + k.encode() + amf(v) for k, v in value.items()) + b"\x00\x00\x09"
    raise ValueError("Unsupported AMF value")


def message(csid, kind, body, *, fmt=0, msid=0):
    if not 2 <= csid < 64 or len(body) > 60000:
        raise ValueError("Invalid RTMP command")
    header = b"\0\0\0" + len(body).to_bytes(3, "big") + bytes([kind])
    if fmt == 0:
        header += struct.pack("<I", msid)
    return bytes([(fmt << 6) | csid]) + header + body


def flv_tag(kind, timestamp, body):
    return bytes([kind]) + len(body).to_bytes(3, "big") + (timestamp & 0xFFFFFF).to_bytes(3, "big") + bytes([(timestamp >> 24) & 255]) + b"\0\0\0" + body + (11 + len(body)).to_bytes(4, "big")


class Receiver:
    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "rtmp" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Unsupported relay")
        # LinkVisual puts query parameters in the application segment, BEFORE
        # the final slash. urlsplit's query handling is intentionally bypassed.
        path = url.split("://", 1)[1].split("/", 1)[1]
        app, self.playpath = path.rsplit("/", 1)
        if "cacheDuration=" not in app:
            app += ("&" if "?" in app else "?") + "cacheDuration=3000"
        self.host, self.port = parsed.hostname, parsed.port or 1935
        self.app, self.tc_url = app, f"rtmp://{self.host}:{self.port}/{app}"
        self.reader = self.writer = None
        self.chunk_size = 60000
        self.streams = {}
        self.transaction = self.received = self.acked = 0
        self.ack_window = 250000
        self.last_ping = 0.0
        self.closed = False

    async def read(self, count):
        result = await asyncio.wait_for(self.reader.readexactly(count), 20)
        self.received += count
        return result

    def command(self, name, *values, csid=3, msid=0):
        self.transaction += 1
        return message(csid, 20, amf(name) + amf(self.transaction) + b"".join(amf(v) for v in values), msid=msid)

    def status(self, code):
        self.transaction += 1
        return message(3, 20, amf("onStatus") + amf(self.transaction) + amf(None) + amf({"level":"status", "code":code, "description":"desc"}), fmt=1)

    async def run(self, output):
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
        c1 = struct.pack(">II", int(time.monotonic() * 1000) & 0xFFFFFFFF, 0) + os.urandom(1528)
        connect = self.command("connect", {"app":self.app, "tcUrl":self.tc_url, "fpad":False, "capabilities":15, "audioCodecs":3191, "videoCodecs":252, "videoFunction":1})
        play = self.command("play", None, self.playpath, -1000, csid=8, msid=1)
        self.writer.write(b"\x04" + c1 + connect + play)
        await self.writer.drain()
        await self.read(1537)  # private S0+S1; this protocol does not send C2
        await output(b"FLV\x01\x01\0\0\0\x09\0\0\0\0")
        try:
            while not self.closed:
                kind, timestamp, body = await self.read_message()
                now = time.monotonic()
                if now - self.last_ping >= 25:
                    self.writer.write(self.status("NetStream.Ping.Start"))
                    self.last_ping = now
                if kind == 1:
                    size = int.from_bytes(body[:4], "big") & 0x7FFFFFFF
                    if not 1 <= size <= 1048576:
                        raise ValueError("Invalid chunk size")
                    self.chunk_size = size
                elif kind == 2 and len(body) >= 4:
                    self.streams.pop(int.from_bytes(body[:4], "big"), None)
                elif kind == 5 and len(body) >= 4:
                    self.ack_window = max(1, min(250000, int.from_bytes(body[:4], "big")))
                elif kind == 4 and body[:2] == b"\x00\x06" and len(body) >= 6:
                    self.writer.write(message(2, 4, b"\x00\x07" + body[2:6]))
                elif kind == 9:
                    await output(flv_tag(kind, timestamp, body))
                if self.closed:
                    break
                if self.received - self.acked >= self.ack_window:
                    self.writer.write(message(2, 3, struct.pack(">I", self.received & 0xFFFFFFFF)))
                    self.acked = self.received
                await self.writer.drain()
        finally:
            self.close()

    async def read_message(self):
        while True:
            first = (await self.read(1))[0]
            fmt, csid = first >> 6, first & 63
            if csid == 0:
                csid = 64 + (await self.read(1))[0]
            elif csid == 1:
                csid = 64 + int.from_bytes(await self.read(2), "little")
            if csid not in self.streams:
                if fmt != 0 or len(self.streams) >= 64:
                    raise ValueError("Invalid chunk stream")
                self.streams[csid] = {"received":0, "parts":[], "timestamp":0, "delta":0, "ext":False}
            state = self.streams[csid]
            stamp = 0
            if fmt <= 2:
                header = await self.read((11, 7, 3)[fmt])
                stamp = int.from_bytes(header[:3], "big")
                if fmt <= 1:
                    state.update(length=int.from_bytes(header[3:6], "big"), kind=header[6])
                    if state["length"] > LIMIT:
                        raise ValueError("RTMP message too large")
                state["ext"] = stamp == 0xFFFFFF
            if state["ext"]:
                stamp = int.from_bytes(await self.read(4), "big")
            if state["received"] == 0:
                if fmt == 0:
                    state.update(timestamp=stamp, delta=0)
                elif fmt <= 2:
                    state["delta"] = stamp
                    state["timestamp"] += stamp
                else:
                    state["timestamp"] += state["delta"]
            take = min(self.chunk_size, state["length"] - state["received"])
            state["parts"].append(await self.read(take))
            state["received"] += take
            if state["received"] == state["length"]:
                body = b"".join(state["parts"])
                state.update(parts=[], received=0)
                return state["kind"], state["timestamp"], body

    def close(self):
        self.closed = True
        if self.writer:
            self.writer.close()
