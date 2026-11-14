"""Treble and bass controls for each AM8328 zone.

Home Assistant's media_player has no tone controls, so these are exposed as
number entities. The amplifier sets both in a single 0x002F frame, so each
entity sends its own new value alongside the other's current value.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Am8328ConfigEntry, Am8328Coordinator
from .const import (
    TONE_DB_PER_STEP,
    TONE_FLAT,
    TONE_MAX,
    TONE_MIN,
    db_to_tone,
    tone_to_db,
)
from .entity import zone_device_info
from .hub import Am8328Error

# The amplifier's eleven raw steps span -10..+10 dB, 2 dB apart.
DISPLAY_MIN = tone_to_db(TONE_MIN)
DISPLAY_MAX = tone_to_db(TONE_MAX)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Am8328ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create treble and bass entities for every zone."""
    coordinator = entry.runtime_data.coordinator
    entities: list[NumberEntity] = []
    for zone in sorted(coordinator.data.zones):
        entities.append(Am8328Tone(coordinator, entry, zone, "treble"))
        entities.append(Am8328Tone(coordinator, entry, zone, "bass"))
    async_add_entities(entities)


class Am8328Tone(CoordinatorEntity[Am8328Coordinator], NumberEntity):
    """One tone control for one zone."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = DISPLAY_MIN
    _attr_native_max_value = DISPLAY_MAX
    _attr_native_step = TONE_DB_PER_STEP
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: Am8328Coordinator,
        entry: Am8328ConfigEntry,
        zone: int,
        kind: str,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._kind = kind
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone}_{kind}"
        # Named from translations; the zone device supplies the room prefix.
        self._attr_translation_key = kind
        self._attr_device_info = zone_device_info(
            entry.entry_id,
            entry.title,
            zone,
            coordinator.data.names.get(zone),
            entry.runtime_data.amp_device_id,
        )

    @property
    def _state(self):
        return self.coordinator.data.zones.get(self._zone)

    @property
    def available(self) -> bool:
        return super().available and self._state is not None

    @property
    def native_value(self) -> float | None:
        if self._state is None:
            return None
        raw = self._state.treble if self._kind == "treble" else self._state.bass
        return tone_to_db(raw)

    async def async_set_native_value(self, value: float) -> None:
        state = self._state
        if state is None:
            return
        raw = db_to_tone(value)

        # 0x002F carries both values, so send the other one unchanged.
        if self._kind == "treble":
            treble, bass = raw, state.bass
            state.treble = raw
        else:
            treble, bass = state.treble, raw
            state.bass = raw

        # Apply to every zone playing with this one, so a group stays matched.
        zones = self.coordinator.group_of(self._zone)
        try:
            for zone in zones:
                zone_state = self.coordinator.data.zones.get(zone)
                if zone_state is None:
                    continue
                if self._kind == "treble":
                    zone_state.treble = raw
                    await self.coordinator.hub.async_set_tone(
                        zone, raw, zone_state.bass
                    )
                else:
                    zone_state.bass = raw
                    await self.coordinator.hub.async_set_tone(
                        zone, zone_state.treble, raw
                    )
        except Am8328Error as err:
            # Only a transport failure gets here; a missing acknowledgement is
            # tolerated because the next 0x0010 poll reports the real value.
            raise HomeAssistantError(f"AM8328 tone update failed: {err}") from err
        self.coordinator.apply_optimistic()
        await self.coordinator.async_request_refresh()
