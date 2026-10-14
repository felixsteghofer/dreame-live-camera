"""One bounded camera session shared by HA viewers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aliyun import VideoIdentity
from .rtmp import Receiver


class LiveCamera:
    def __init__(self, hass, cloud, pin, duration):
        self.hass, self.cloud, self.pin, self.duration = hass, cloud, pin, duration
        self.active = False
        self.stage = "Off"
        self.frames = 0
        self.image = None
        self.listeners = set()
        self.viewers = set()
        self.condition = asyncio.Condition()
        self.lock = asyncio.Lock()
        self.session = None
        self.receiver = self.process = None
        self.tasks = []
        self.lease = self.grace = None
        self.cleanup_ok = True

    def notify(self):
        for listener in tuple(self.listeners):
            listener()

    async def call(self, function, *args):
        return await self.hass.async_add_executor_job(function, *args)

    async def state(self):
        rows = await self.call(self.cloud.get_properties, "10001.1")
        for row in rows or []:
            if row.get("key") == "10001.1":
                value = row.get("value")
                return json.loads(value) if isinstance(value, str) else value or {}
        raise RuntimeError("Camera status unavailable")

    async def action(self, aiid, piid, value):
        if (aiid, piid) not in {(4, 1100), (7, 11), (1, 1), (4, 1102), (1, 6)}:
            raise RuntimeError("Camera operation not supported")
        params = {"did":str(self.cloud.device_id), "siid":10001, "aiid":aiid, "in":[{"piid":piid, "value":json.dumps({**value, "session":self.session}, separators=(",", ":"))}]}
        return await self.call(self.cloud.send, "action", params, 0, 10)

    def background(self, coro, name):
        return self.hass.async_create_background_task(coro, f"dreame_live_camera {name}")

    async def async_start(self):
        async with self.lock:
            if self.active:
                return
            self.stage = "Connecting"
            self.notify()
            try:
                async with asyncio.timeout(75):
                    current = await self.state()
                    if current.get("status") == 1:
                        raise RuntimeError("Close the camera in the manufacturer app first")
                    if not self.cloud._stream_key:
                        await self.call(self.cloud.get_device_info)
                    if not self.cloud._stream_key:
                        raise RuntimeError("Device has no LinkVisual channel")
                    channel_id = self.cloud._stream_key
                    identity = VideoIdentity(async_get_clientsession(self.hass), self.cloud._country)
                    response = await self.call(self.cloud._api_call, "dreame-smarthome/aliIot/getAuthCodeV3", {"os":"android"}, 0, 12)
                    if not response or response.get("code") != 0:
                        raise RuntimeError("Video account authentication failed")
                    await identity.authenticate(response["data"])
                    self.session = hashlib.md5(f"{self.cloud._uuid}_{int(time.time()*1000)}".encode()).hexdigest()
                    self.cleanup_ok = False
                    for aiid, piid, params in [(4,1100,{"open":True}), (7,11,{})]:
                        await self.action(aiid, piid, params)
                    params = {"token":"alify", "channelId":channel_id, "area":"4", "operType":"monitor", "operation":"start"}
                    result = await self.action(1, 1, params)
                    if not result or result.get("code") != 0:
                        if not self.pin:
                            raise RuntimeError("Configure the camera PIN in integration options")
                        verified = await self.action(4, 1102, {"oldcode":hashlib.sha256(self.pin.encode()).hexdigest(), "lazymode":0})
                        if not verified or verified.get("code") != 0:
                            raise RuntimeError("Camera PIN rejected")
                        result = await self.action(1, 1, params)
                    if not result or result.get("code") != 0:
                        raise RuntimeError("Camera startup rejected")
                    self.active = True
                    self.frames, self.image = 0, None
                    self.tasks.append(self.background(self.keepalive(), "keepalive"))
                    self.lease = asyncio.get_running_loop().call_later(self.duration, lambda:self.background(self.async_stop("Session limit reached"), "limit"))
                    for attempt in range(5):
                        try:
                            url = await identity.relay(channel_id)
                            break
                        except Exception:
                            if attempt == 4:
                                raise RuntimeError("Camera relay unavailable") from None
                            await asyncio.sleep(1)
                    self.process = await asyncio.create_subprocess_exec("/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "quiet", "-probesize", "32768", "-analyzeduration", "0", "-f", "flv", "-i", "pipe:0", "-an", "-vf", "fps=3,scale=640:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-flush_packets", "1", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    self.receiver = Receiver(url)
                    self.tasks.append(self.background(self.media(), "media"))
                    self.tasks.append(self.background(self.decode(), "decoder"))
                    async with self.condition:
                        await asyncio.wait_for(self.condition.wait_for(lambda:self.image is not None or not self.active), 20)
                    if self.image is None:
                        raise RuntimeError("No video frames received")
                    self.stage = "Live"
                    self.notify()
                    # If the user turns it on but never opens a viewer, end it.
                    self.grace = asyncio.get_running_loop().call_later(30, lambda:self.background(self.stop_if_unwatched(), "idle"))
            except BaseException as error:
                await self._stop("Camera connection failed")
                if isinstance(error, asyncio.CancelledError):
                    raise
                safe = str(error) if isinstance(error, RuntimeError) else "Camera connection failed"
                raise RuntimeError(safe) from None

    async def keepalive(self):
        try:
            while self.active:
                await asyncio.sleep(10)
                state = await self.state()
                if state.get("session") != self.session:
                    raise RuntimeError("Camera session ownership changed")
                result = await self.action(1, 6, {"operType":"keep_alive", "videoStatus":"opened"})
                if not result or result.get("code") != 0:
                    raise RuntimeError("Camera keep-alive rejected")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.async_stop("Camera keep-alive ended")

    async def media(self):
        async def output(data):
            self.process.stdin.write(data)
            await self.process.stdin.drain()
        try:
            await self.receiver.run(output)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.async_stop("Video transport ended")

    async def decode(self):
        buffered = b""
        try:
            while self.active:
                chunk = await self.process.stdout.read(65536)
                if not chunk:
                    raise RuntimeError("Decoder ended")
                buffered += chunk
                while True:
                    start = buffered.find(b"\xff\xd8")
                    if start < 0:
                        buffered = buffered[-1:]
                        break
                    buffered = buffered[start:]
                    end = buffered.find(b"\xff\xd9", 2)
                    if end < 0:
                        break
                    self.image, buffered = buffered[:end+2], buffered[end+2:]
                    self.frames += 1
                    async with self.condition:
                        self.condition.notify_all()
                    if self.frames == 1 or self.frames % 15 == 0:
                        self.notify()
                if len(buffered) > 4*1024*1024:
                    raise RuntimeError("Video frame too large")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.async_stop("Video decoder ended")

    async def stop_if_unwatched(self):
        if not self.viewers:
            await self.async_stop("No viewers")

    def viewer_open(self):
        key = object()
        self.viewers.add(key)
        if self.grace:
            self.grace.cancel()
        return key

    def viewer_close(self, key):
        self.viewers.discard(key)
        if not self.viewers and self.active:
            self.grace = asyncio.get_running_loop().call_later(2, lambda:self.background(self.stop_if_unwatched(), "disconnect"))

    async def async_stop(self, reason="Off"):
        async with self.lock:
            await self._stop(reason)

    async def _stop(self, reason):
        self.active = False
        for handle in (self.lease, self.grace):
            if handle:
                handle.cancel()
        self.lease = self.grace = None
        tasks, self.tasks = self.tasks, []
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not current), return_exceptions=True)
        if self.receiver:
            self.receiver.close()
        self.receiver = None
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            self.process = None
        if self.session:
            try:
                state = await self.state()
                if state.get("session") == self.session and state.get("status") == 1:
                    result = await self.action(1, 1, {"operType":"monitor", "operation":"end"})
                    if not result or result.get("code") != 0:
                        raise RuntimeError("Stop rejected")
                self.cleanup_ok = True
            except Exception:
                reason = "Camera cleanup could not be confirmed"
                self.cleanup_ok = False
            self.session = None
        self.image = None
        self.stage = reason
        self.notify()
        async with self.condition:
            self.condition.notify_all()
