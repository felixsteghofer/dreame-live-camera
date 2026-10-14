"""Explicit camera controls; never drive or clean."""
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([CameraButton(entry, True), CameraButton(entry, False)])


class CameraButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry, start):
        self.manager, self.start = entry.runtime_data, start
        self._attr_unique_id = f"{entry.unique_id}_live_camera_{'start' if start else 'stop'}"
        self._attr_name = f"{entry.title.removesuffix(' Live Camera')} Camera {'Start' if start else 'Stop'}"
        self._attr_icon = "mdi:video" if start else "mdi:video-off"

    async def async_press(self):
        try:
            if self.start:
                await self.manager.async_start()
            else:
                await self.manager.async_stop()
        except Exception as error:
            raise HomeAssistantError(str(error)) from None
