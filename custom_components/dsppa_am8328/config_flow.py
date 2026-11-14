"""Config flow for the DSPPA AM8328."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AIRPLAY_ENTITIES,
    CONF_AUTO_ACTIVATE,
    CONF_LEAVE_SOURCE,
    DEFAULT_LEAVE_SOURCE,
    CONF_DLNA_ENTITIES,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    airplay_key,
    dlna_key,
)
from .hub import Am8328Error, Am8328Hub

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
    }
)


class CannotConnect(Exception):
    """Socket could not be opened."""


class NoResponse(Exception):
    """Amplifier did not answer."""


async def _async_validate(data: dict[str, Any]) -> list[str]:
    """Connect and read the model/firmware string back."""
    hub = Am8328Hub(data[CONF_HOST], int(data[CONF_PORT]))
    try:
        await hub.async_connect()
    except Am8328Error as err:
        raise CannotConnect(str(err)) from err
    try:
        info = await hub.async_get_info()
        if not info:
            raise NoResponse("no reply to the device info query")
        return info
    finally:
        hub.close()


class Am8328ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            try:
                info = await _async_validate(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoResponse:
                errors["base"] = "no_response"
            else:
                title = user_input.get(CONF_NAME) or info[0]
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )


    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "Am8328OptionsFlow":
        return Am8328OptionsFlow()


class Am8328OptionsFlow(OptionsFlow):
    """Link each zone to its own DLNA renderer.

    The amplifier exposes one UPnP MediaRenderer per zone, so media played to a
    zone can be handed to that zone's renderer - giving genuinely independent
    playback in every room.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones, names = self._zones_and_names()

        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_DLNA_ENTITIES: {
                        str(zone): user_input[dlna_key(zone)]
                        for zone in zones
                        if user_input.get(dlna_key(zone))
                    },
                    CONF_AIRPLAY_ENTITIES: {
                        str(zone): user_input[airplay_key(zone)]
                        for zone in zones
                        if user_input.get(airplay_key(zone))
                    },
                    CONF_AUTO_ACTIVATE: user_input.get(CONF_AUTO_ACTIVATE, True),
                    CONF_LEAVE_SOURCE: int(
                        user_input.get(CONF_LEAVE_SOURCE, DEFAULT_LEAVE_SOURCE)
                    ),
                }
            )

        options = self.config_entry.options
        current = options.get(CONF_DLNA_ENTITIES, {})
        airplay = options.get(CONF_AIRPLAY_ENTITIES, {})
        fields: dict[Any, Any] = {}
        self._labels = {}

        fields[
            vol.Optional(
                CONF_AUTO_ACTIVATE,
                description={
                    "suggested_value": options.get(CONF_AUTO_ACTIVATE, True)
                },
            )
        ] = BooleanSelector()

        fields[
            vol.Optional(
                CONF_LEAVE_SOURCE,
                description={
                    "suggested_value": str(
                        options.get(CONF_LEAVE_SOURCE, DEFAULT_LEAVE_SOURCE)
                    )
                },
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=str(value), label=label)
                    for value, label in self._source_choices().items()
                ]
            )
        )

        for zone in zones:
            # Show the amplifier's own zone name, not "zone_3".
            self._labels[dlna_key(zone)] = (
                f"Zone {zone} = {names.get(zone) or 'unnamed'}"
            )
            fields[
                vol.Optional(
                    dlna_key(zone),
                    description={"suggested_value": current.get(str(zone))},
                )
            ] = EntitySelector(EntitySelectorConfig(domain="media_player"))
            fields[
                vol.Optional(
                    airplay_key(zone),
                    description={"suggested_value": airplay.get(str(zone))},
                )
            ] = EntitySelector(EntitySelectorConfig(domain="media_player"))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "zone_names": "\n".join(f"- {label}" for label in self._labels.values())
            },
        )

    def _source_choices(self) -> dict[int, str]:
        """Sources this amplifier reports, for the leave-group setting."""
        data = getattr(self.config_entry, "runtime_data", None)
        choices: dict[int, str] = {}
        if data is not None and data.coordinator.data:
            choices.update(data.coordinator.data.aliases)
        if not choices:
            choices = {0x81: "SD/USB", 0x41: "DVD", 0x51: "AUX1", 0x52: "AUX2"}
        return choices

    def _zones_and_names(self) -> tuple[list[int], dict[int, str]]:
        """Zone numbers and names, from the running integration if available."""
        data = getattr(self.config_entry, "runtime_data", None)
        if data is not None and data.coordinator.data:
            snapshot = data.coordinator.data
            return sorted(snapshot.zones), dict(snapshot.names)
        return list(range(1, 13)), {}
