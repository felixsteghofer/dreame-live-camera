"""HA-authenticated MJPEG camera; previews never activate hardware."""
import asyncio

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.exceptions import HomeAssistantError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DreameLiveCamera(entry)])


class DreameLiveCamera(Camera):
    _attr_should_poll = False
    _attr_supported_features = CameraEntityFeature.ON_OFF
    _attr_icon = "mdi:video"
    _attr_frame_interval = 1 / 3

    def __init__(self, entry):
        super().__init__()
        self.manager = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_live_camera"
        self._attr_name = f"{entry.title.removesuffix(' Live Camera')} Live"

    @property
    def is_on(self):
        return self.manager.active

    @property
    def is_streaming(self):
        return self.manager.active

    @property
    def extra_state_attributes(self):
        manager = self.manager
        lan = manager.receiver if manager.transport == "direct_lan" else None
        relay = manager.receiver if manager.transport == "cloud_relay" else None
        return {"status":manager.stage, "decoded_frames":manager.frames,
                "viewers":len(manager.viewers), "cleanup_confirmed":manager.cleanup_ok,
                "max_duration":manager.duration, "video_transport":manager.transport,
                "cloud_setup_required":True, "lan_peer":lan.peer if lan else manager.last_peer,
                "received_datagrams":lan.datagrams if lan else manager.last_datagrams,
                "relay_bytes":relay.received if relay else manager.last_relay_bytes,
                "fallback_reason":manager.fallback_reason,
                "media_cleanup_confirmed":manager.media_cleanup_ok}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.manager.listeners.add(self.async_write_ha_state)
        self.async_on_remove(lambda:self.manager.listeners.discard(self.async_write_ha_state))

    async def async_camera_image(self, width=None, height=None):
        return self.manager.image  # Never call start for previews/snapshots.

    async def async_turn_on(self):
        try:
            await self.manager.async_start()
        except Exception as error:
            raise HomeAssistantError(str(error)) from None

    async def async_turn_off(self):
        await self.manager.async_stop()

    async def handle_async_still_stream(self, request, interval):
        manager = self.manager
        if not manager.active:
            raise web.HTTPServiceUnavailable(text="Turn on the live camera first")
        key = manager.viewer_open()
        response = web.StreamResponse(headers={"Content-Type":"multipart/x-mixed-replace; boundary=frame", "Cache-Control":"no-store"})
        try:
            await response.prepare(request)
            previous = -1
            while manager.active:
                async with manager.condition:
                    await asyncio.wait_for(manager.condition.wait_for(lambda:manager.frames != previous or not manager.active), 20)
                if not manager.active:
                    break
                image, previous = manager.image, manager.frames
                if image:
                    part = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(image)}\r\n\r\n".encode() + image + b"\r\n"
                    await asyncio.wait_for(response.write(part), 5)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            manager.viewer_close(key)
        return response
