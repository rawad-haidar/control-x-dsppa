"""The DSPPA AM8328 integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .entity import amplifier_device_info
from .const import DEFAULT_PORT, DOMAIN, PLAY, SCAN_INTERVAL, STOP
from .hub import Am8328Error, Am8328Hub
from .protocol import ZoneState

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.MEDIA_PLAYER, Platform.NUMBER]

type Am8328ConfigEntry = ConfigEntry["Am8328Data"]


@dataclass
class Am8328Snapshot:
    """Everything the entities need, refreshed each poll."""

    zones: dict[int, ZoneState] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    aliases: dict[int, str] = field(default_factory=dict)
    text: dict[int, str] = field(default_factory=dict)
    play_state: dict[int, int | None] = field(default_factory=dict)
    folders: dict[int, str] = field(default_factory=dict)


@dataclass
class Am8328Data:
    hub: Am8328Hub
    coordinator: "Am8328Coordinator"
    info: list[str]
    amp_device_id: str | None = None


class Am8328Coordinator(DataUpdateCoordinator[Am8328Snapshot]):
    """Polls the amplifier.

    Zone state arrives in a single frame, so a full refresh is one round trip.
    Names and source aliases change rarely and are fetched once.
    """

    def __init__(self, hass: HomeAssistant, hub: Am8328Hub) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.hub = hub
        self.control_lock = asyncio.Lock()
        self._snapshot = Am8328Snapshot()
        self._static_loaded = False
        self._text_cursor = 0
        self._track_ended: list = []
        # Zone grouping: leader zone -> member zones (leader included first).
        self.groups: dict[int, list[int]] = {}
        # Zones the user has explicitly removed from their derived group. The
        # hardware may feed one renderer to several zones, but a zone taken out
        # here stays out until it is joined again.
        self.parked: set[int] = set()
        # Zones that received a play request recently. Music Assistant plays to
        # grouped players by calling each one separately within a second or so;
        # a zone in this window is treated as wanted and is never parked.
        self.recent_play: dict[int, float] = {}
        # Last media started per renderer, so a renderer shared by several zones
        # is not restarted once per zone.
        self.renderer_started: dict[str, tuple[str, float]] = {}
        # Entity ids by zone, so grouping can translate between the two.
        self.parked: set[int] = set()
        self.zone_entities: dict[int, str] = {}
        hub.set_now_playing_callback(self._handle_now_playing)

    def _handle_now_playing(self, zone: int, title: str, state: int) -> None:
        """Apply a pushed programme change straight away."""
        if zone not in self._snapshot.zones:
            return
        previous = self._snapshot.play_state.get(zone)
        self._snapshot.text[zone] = title
        self._snapshot.play_state[zone] = state
        self.async_set_updated_data(self._snapshot)

        # A track that has just stopped may have a queue waiting behind it.
        if previous == PLAY and state == STOP:
            for callback in self._track_ended:
                callback(zone)

    def group_of(self, zone: int) -> list[int]:
        """Every zone playing together with this one, leader first."""
        for leader, members in self.groups.items():
            if zone in members:
                return members
        return [zone]

    def set_group(self, leader: int, members: list[int]) -> None:
        """Form a group, removing its members from any group they were in."""
        members = list(dict.fromkeys([leader, *members]))
        self.parked.difference_update(members)
        for other, existing in list(self.groups.items()):
            if other == leader:
                continue
            remaining = [z for z in existing if z not in members]
            self.groups.pop(other)
            if len(remaining) > 1:
                self.groups[remaining[0]] = remaining
        self.groups[leader] = members
        self.apply_optimistic()

    def clear_group(self, zone: int) -> None:
        """Remove only this zone, promoting a remaining member if needed."""
        self.parked.add(zone)
        for leader, members in list(self.groups.items()):
            if zone not in members:
                continue
            remaining = [z for z in members if z != zone]
            self.groups.pop(leader)
            if len(remaining) > 1:
                self.groups[remaining[0]] = remaining
        self.apply_optimistic()

    def add_track_ended_listener(self, callback):
        """Register a zone callback for tracks reaching their end.

        Returns a function that unregisters it again, so entities can clean up
        on reload.
        """
        self._track_ended.append(callback)

        def _remove() -> None:
            if callback in self._track_ended:
                self._track_ended.remove(callback)

        return _remove

    async def _async_update_data(self) -> Am8328Snapshot:
        try:
            self._snapshot.zones = await self.hub.async_get_state()
        except Am8328Error as err:
            raise UpdateFailed(str(err)) from err

        if not self._static_loaded:
            self._snapshot.names = await self.hub.async_get_zone_names()
            self._snapshot.aliases = await self.hub.async_get_source_aliases()
            self._snapshot.folders = await self.hub.async_get_folders()
            self._static_loaded = True

        # Play state and now-playing text are per-zone queries. Refresh a few
        # zones per cycle so a 12-zone system does not need 24 round trips
        # every time.
        zones = sorted(self._snapshot.zones)
        for _ in range(min(4, len(zones))):
            if not zones:
                break
            zone = zones[self._text_cursor % len(zones)]
            self._text_cursor += 1
            try:
                self._snapshot.play_state[zone] = (
                    await self.hub.async_get_play_state(zone)
                )
                self._snapshot.text[zone] = await self.hub.async_get_text(zone)
            except Am8328Error:
                pass

        return self._snapshot

    def apply_optimistic(self) -> None:
        """Publish locally-updated state without waiting for the next poll."""
        self.async_set_updated_data(self._snapshot)


async def async_setup_entry(hass: HomeAssistant, entry: Am8328ConfigEntry) -> bool:
    """Set up the AM8328 from a config entry."""
    hub = Am8328Hub(entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT))
    try:
        await hub.async_connect()
        info = await hub.async_get_info()
    except Am8328Error as err:
        hub.close()
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = Am8328Coordinator(hass, hub)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        hub.close()
        raise

    entry.runtime_data = Am8328Data(hub=hub, coordinator=coordinator, info=info)
    # Register the amplifier itself so zone devices can link to it by registry id.
    amp = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **amplifier_device_info(entry.entry_id, entry.title, info),
    )
    entry.runtime_data.amp_device_id = amp.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: Am8328ConfigEntry) -> None:
    """Reload when the DLNA link is changed in the options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Am8328ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.hub.close()
    return unloaded
