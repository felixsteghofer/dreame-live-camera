"""Independent live-camera companion for Dreame Vacuum."""
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .manager import LiveCamera

DOMAIN = "dreame_live_camera"


async def async_setup_entry(hass, entry):
    vacuum = er.async_get(hass).async_get(entry.data["vacuum_entity_id"])
    if not vacuum or vacuum.platform != "dreame_vacuum":
        raise ConfigEntryNotReady("Configured Dreame vacuum is not available")
    coordinator = hass.data.get("dreame_vacuum", {}).get(vacuum.config_entry_id)
    device = getattr(coordinator, "_device", None)
    cloud = getattr(getattr(device, "_protocol", None), "cloud", None)
    if cloud is None or not getattr(cloud, "logged_in", False):
        raise ConfigEntryNotReady("Dreame/MOVA cloud connection is not ready")
    manager = LiveCamera(hass, cloud, entry.options.get("pin", entry.data.get("pin", "")), entry.options.get("max_duration", entry.data.get("max_duration", 120)))
    entry.runtime_data = manager
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda event:hass.async_create_task(manager.async_stop())))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, ["camera", "button"])
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, ["camera", "button"])
