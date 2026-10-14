"""UI configuration. The PIN stays in HA's config entry, never a state attribute."""
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

DOMAIN = "dreame_live_camera"


def schema(defaults, include_vacuum=True):
    fields = {}
    if include_vacuum:
        fields[vol.Required("vacuum_entity_id", default=defaults.get("vacuum_entity_id", ""))] = selector.EntitySelector(selector.EntitySelectorConfig(domain="vacuum", integration="dreame_vacuum"))
    # Never prefill a saved PIN into form responses.
    fields[vol.Optional("pin")] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
    fields[vol.Required("max_duration", default=defaults.get("max_duration", 120))] = vol.All(vol.Coerce(int), vol.Range(min=30, max=600))
    return vol.Schema(fields)


def valid_pin(value):
    return not value or bool(re.fullmatch(r"\d{4,12}", value))


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            entity = er.async_get(self.hass).async_get(user_input["vacuum_entity_id"])
            if not entity or entity.platform != "dreame_vacuum":
                errors["base"] = "invalid_vacuum"
            elif not valid_pin(user_input.get("pin")):
                errors["pin"] = "invalid_pin"
            else:
                await self.async_set_unique_id(entity.unique_id)
                self._abort_if_unique_id_configured()
                state = self.hass.states.get(entity.entity_id)
                name = entity.entity_id.split(".", 1)[1].replace("_", " ").title()
                return self.async_create_entry(title=f"{name} Live Camera", data=user_input)
        return self.async_show_form(step_id="user", data_schema=schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            if not valid_pin(user_input.get("pin")):
                return self.async_show_form(step_id="init", data_schema=schema(user_input, False), errors={"pin":"invalid_pin"})
            return self.async_create_entry(title="", data={**self.config_entry.options, **user_input})
        return self.async_show_form(step_id="init", data_schema=schema({**self.config_entry.data, **self.config_entry.options}, False))
