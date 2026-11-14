"""Media player entities for the DSPPA AM8328 — one per zone."""

from __future__ import annotations

import asyncio
import logging
import time
import random
import math
from datetime import datetime

from homeassistant.util import dt as dt_util
from urllib.parse import urljoin

import aiohttp

from homeassistant.components.media_player import (
    BrowseMedia,
    RepeatMode,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components import media_source
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Am8328ConfigEntry, Am8328Coordinator
from .const import (
    MAX_VOLUME,
    NETWORK_SOURCES,
    PAUSE,
    PLAY,
    CONF_AIRPLAY_ENTITIES,
    CONF_AUTO_ACTIVATE,
    CONF_LEAVE_SOURCE,
    DEFAULT_LEAVE_SOURCE,
    CONF_DLNA_ENTITIES,
    CONF_DLNA_ENTITY,
    MAX_STREAM_PAYLOAD,
    RADIO_BASE,
    RADIO_MASK,
    STEP_NEXT,
    STEP_PREVIOUS,
    SOURCE_DLNA,
    is_dlna_source,
    SOURCE_SDUSB,
    airplay_key,
    dlna_key,
    STOP,
    source_label,
)
from .entity import zone_device_info
from .hub import Am8328Error

FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
)

# media_content_id for a track is "folder|track"; folders use "folder:<name>".
FOLDER_PREFIX = "folder:"
SDUSB_ROOT = "sdusb"

# The amplifier's streaming client is a plain embedded MP3/AAC player.
STREAM_SOURCE = 0xC1  # net radio - the mode that plays a pushed URL

_LOGGER = logging.getLogger(__name__)

VOLUME_STEP = 5
ANNOUNCE_VOLUME = 0.5  # 50% volume for announcements
RECENT_PLAY_WINDOW = 5.0  # seconds a zone counts as "just asked to play"
RENDERER_DEDUPE_WINDOW = 3.0  # skip restarting a shared renderer within this

# Zones fed by an analogue line input instead of a DLNA renderer.
#   zone number -> (amplifier source name, Home Assistant player wired to it)
# Zone 1 (Gym) listens on AUX1, which is cabled to the server's audio jack.
# Anything played to such a zone is handed to the player named here and the
# zone is switched to that input. The zone is also taken off the shared DLNA
# renderer, so it is never parked and never woken by the other rooms.
# The source may be the name shown in the zone's source list, or its raw value.
LINE_IN_PLAYERS: dict[int, tuple[str | int, str]] = {
    1: ("AUX1", "media_player.vlc_telnet"),
}
LINE_IN_OWN_WINDOW = 30.0  # seconds a line-in start counts as this zone's own


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Am8328ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one entity per zone the amplifier reported."""
    data = entry.runtime_data
    zones = sorted(data.coordinator.data.zones)
    async_add_entities(
        Am8328Zone(data.coordinator, entry, zone) for zone in zones
    )


class Am8328Zone(CoordinatorEntity[Am8328Coordinator], MediaPlayerEntity):
    """A single amplifier zone."""

    _attr_has_entity_name = True
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER

    def __init__(
        self, coordinator: Am8328Coordinator, entry: Am8328ConfigEntry, zone: int
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone}"
        # The entity carries the device's name, so the zone shows up as e.g.
        # media_player.gym rather than media_player.am8328_gym.
        self._attr_name = None
        # Playback queue: DLNA renderers stop at the end of each track, so the
        # integration holds the list and feeds the next one.
        self._queue: list[str] = []
        self._media_extras: dict[str, dict] = {}
        self._queue_type: str = MediaType.MUSIC
        self._queue_index: int = 0
        self._last_media_type: str = ""
        self._last_media_id: str = ""
        self._announce_saved_volume: float | None = None
        # When this zone last started its line-in player itself.
        self._line_in_play_at: float = float("-inf")
        self._attr_repeat = RepeatMode.OFF
        self._attr_shuffle = False
        self._attr_device_info = zone_device_info(
            entry.entry_id,
            entry.title,
            zone,
            coordinator.data.names.get(zone),
            entry.runtime_data.amp_device_id,
        )

    async def async_added_to_hass(self) -> None:
        """Follow this zone's linked players.

        Their state is mirrored onto the zone, and when one of them starts
        playing the zone is woken up and pointed at the network input - so
        casting to a room's AirPlay or DLNA endpoint just works, without
        having to switch the amplifier by hand first.
        """
        await super().async_added_to_hass()
        self.coordinator.zone_entities[self._zone] = self.entity_id

        @callback
        def _track_ended(zone: int) -> None:
            # The amplifier reports its own stops for SD/USB and pushed streams.
            if (
                zone == self._zone
                and not (self._dlna_entity and self._on_dlna)
                and not self._on_line_in
            ):
                self.hass.async_create_task(self._async_advance())

        self.async_on_remove(
            self.coordinator.add_track_ended_listener(_track_ended)
        )

        @callback
        def _music_assistant_changed(event) -> None:
            # Metadata only: never wake zones or send playback commands here.
            entity_id = event.data.get("entity_id")
            registry = er.async_get(self.hass)
            entry = registry.async_get(entity_id) if entity_id else None
            if entry and entry.platform == "music_assistant":
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, _music_assistant_changed)
        )

        line_in = self._line_in_entity
        if line_in:

            @callback
            def _line_in_changed(event) -> None:
                new = event.data.get("new_state")
                old = event.data.get("old_state")
                was_playing = (
                    old is not None and old.state == MediaPlayerState.PLAYING
                )

                if new is not None and new.state == MediaPlayerState.PLAYING:
                    if not was_playing:
                        resumed = (
                            old is not None
                            and old.state == MediaPlayerState.PAUSED
                        )
                        ours = (
                            time.monotonic() - self._line_in_play_at
                            < LINE_IN_OWN_WINDOW
                        )
                        if not ours and not resumed:
                            # Started by something else, e.g. Music Assistant
                            # playing straight to the player. This zone's own
                            # queue is stale and must not take over later.
                            self._queue = []
                            self._queue_index = 0
                        # Whoever started it, the room should hear it.
                        self.hass.async_create_task(
                            self._async_line_in_activate()
                        )
                elif (
                    was_playing
                    and self._on_line_in
                    and new is not None
                    and new.state in (MediaPlayerState.IDLE, MediaPlayerState.OFF)
                ):
                    # The track finished. Move on rather than falling silent.
                    self.hass.async_create_task(self._async_advance())

                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [line_in], _line_in_changed
                )
            )

        watched = [e for e in (self._dlna_entity, self._airplay_entity) if e]
        if not watched:
            return

        @callback
        def _linked_changed(event) -> None:
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            was_playing = old is not None and old.state == MediaPlayerState.PLAYING

            if new is not None and new.state == MediaPlayerState.PLAYING:
                if not was_playing:
                    self.hass.async_create_task(self._async_auto_activate())
            elif was_playing and new is not None and new.state in (
                MediaPlayerState.IDLE,
                MediaPlayerState.OFF,
            ):
                # The track finished. Move on rather than falling silent.
                self.hass.async_create_task(self._async_advance())

            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(self.hass, watched, _linked_changed)
        )



    async def _async_auto_activate(self) -> None:
        """React to a linked player starting, if the user wants that.

        For a renderer shared by several zones, playback starting tells us
        nothing about WHICH room wanted it - all five would wake and undo any
        parking. So a zone on a shared renderer only wakes itself if it was
        the one asked to play (or is grouped with that one).
        """
        if not self._entry.options.get(CONF_AUTO_ACTIVATE, True):
            return
        if self._zone in self.coordinator.parked:
            return
        siblings = self._sibling_zones()
        if siblings:
            now = time.monotonic()
            wanted = {
                z for z, t in self.coordinator.recent_play.items()
                if now - t < RECENT_PLAY_WINDOW
            }
            on_renderer = {self._zone, *siblings}
            # A specific room on this renderer was just asked to play: only it
            # (and whatever is grouped with it) should wake. With no request at
            # all - e.g. casting from a phone - fall through and wake as before.
            if wanted & on_renderer and not (
                set(self.coordinator.group_of(self._zone)) & wanted
            ):
                return
        async with self.coordinator.control_lock:
            if self._zone not in self.coordinator.parked:
                await self._async_wake_locked(SOURCE_DLNA)

    async def _async_line_in_activate(self) -> None:
        """The line-in player started: power the zone on and select its input."""
        if not self._entry.options.get(CONF_AUTO_ACTIVATE, True):
            return
        source = self._line_in_source
        if source is None:
            return
        await self._async_wake(source)

    async def _async_wake(self, source: int) -> None:
        async with self.coordinator.control_lock:
            await self._async_wake_locked(source)

    async def _async_wake_locked(self, source: int) -> None:
        """Power the zone on and point it at the source about to be used.

        Applies to every zone in the group, so playing to one room brings the
        whole group with it. Used both when a linked player starts on its own
        and when Home Assistant plays something here.
        """
        self.coordinator.parked.difference_update(self._group_zones)
        try:
            for zone in self._group_zones:
                state = self.coordinator.data.zones.get(zone)
                if state is None:
                    continue
                if not state.power:
                    state.power = True
                    await self.coordinator.hub.async_set_power(zone, True)
                if state.source != source and not (
                    is_dlna_source(source) and is_dlna_source(state.source)
                ):
                    state.source = source
                    await self.coordinator.hub.async_set_source(zone, source)
        except Am8328Error as err:
            _LOGGER.warning(
                "Could not wake zone %s for playback: %s", self._zone, err
            )
            return
        self.coordinator.apply_optimistic()

    @property
    def _state(self):
        return self.coordinator.data.zones.get(self._zone)

    @property
    def available(self) -> bool:
        return super().available and self._state is not None

    @property
    def state(self) -> MediaPlayerState | None:
        if self._state is None:
            return None
        if not self._state.power:
            return MediaPlayerState.OFF
        # On DLNA or a line input the linked player knows what is happening,
        # not the amplifier.
        renderer = self._linked_state()
        if renderer and renderer.state in (
            MediaPlayerState.PLAYING,
            MediaPlayerState.PAUSED,
            MediaPlayerState.IDLE,
        ):
            return MediaPlayerState(renderer.state)
        return {
            PLAY: MediaPlayerState.PLAYING,
            PAUSE: MediaPlayerState.PAUSED,
            STOP: MediaPlayerState.IDLE,
        }.get(self.coordinator.data.play_state.get(self._zone), MediaPlayerState.ON)

    @property
    def volume_level(self) -> float | None:
        return None if self._state is None else self._state.volume / MAX_VOLUME

    @property
    def source_list(self) -> list[str]:
        """The sources this amplifier actually has.

        0x0022 reports the physical inputs it was configured with (SD/USB,
        DVD, AUX1, AUX2). The two network sources are not in that table but do
        appear in the app's picker, so they are appended. The full DSPPA
        source table is deliberately NOT merged in - this model has no FM,
        Bluetooth or AirPlay, and offering them would just fail.
        """
        aliases = self.coordinator.data.aliases
        names = [aliases[v] for v in sorted(aliases) if aliases[v]]
        if not names:
            names = ["SD/USB", "DVD", "AUX1", "AUX2"]
        return names + list(NETWORK_SOURCES.values())

    @property
    def source(self) -> str | None:
        if self._state is None:
            return None
        value = self._state.source
        if is_dlna_source(value):
            return NETWORK_SOURCES[SOURCE_DLNA]
        # NETWORK_SOURCES wins over the generic table, so 0xB1 reads as
        # "Internet music" rather than "DLNA".
        if value in NETWORK_SOURCES:
            return NETWORK_SOURCES[value]
        alias = self.coordinator.data.aliases.get(value)
        if alias:
            return alias
        if value & RADIO_MASK == RADIO_BASE:
            return NETWORK_SOURCES[0xC1]
        return source_label(value)

    def _music_assistant_state(self):
        """Find live metadata for this output, never by a room's display name."""
        if not (self._on_dlna or self._on_line_in) or not self._state.power:
            return None
        registry = er.async_get(self.hass)
        targets = [self.entity_id]
        targets.extend(
            self.coordinator.zone_entities[z] for z in self._group_zones
            if z in self.coordinator.zone_entities
        )
        if self._dlna_entity:
            targets.append(self._dlna_entity)
        if self._on_line_in:
            targets.append(self._line_in_entity)
        # Ungrouped zones may still be listening to the same hardware stream.
        targets.extend(
            self.coordinator.zone_entities[z] for z in self._sibling_zones()
            if z in self.coordinator.zone_entities
            and self.coordinator.data.zones[z].power
            and is_dlna_source(self.coordinator.data.zones[z].source)
        )
        for target in dict.fromkeys(targets):
            entity_id = registry.async_get_entity_id(
                "media_player", "music_assistant", target
            )
            state = self.hass.states.get(entity_id) if entity_id else None
            if (
                state and state.state in ("playing", "paused")
                and state.attributes.get("active_queue")
            ):
                return state
        return None

    def _metadata_state(self):
        """Prefer Music Assistant's live queue over the continuous stream."""
        return self._music_assistant_state() or self._linked_state()

    @property
    def media_title(self) -> str | None:
        """Show the real programme name, not a generic label.

        Priority depends on what the zone is doing:
        - On DLNA / Internet music: the renderer knows what is playing, and
          the amplifier's text is often stale from a previous stream push.
          Renderer first, amplifier second.
        - On anything else (radio, SD/USB, line in): the amplifier's own
          now-playing text is the truth. Renderer is irrelevant.
        """
        live = self._music_assistant_state()
        if live and live.attributes.get("media_title"):
            return live.attributes["media_title"]
        line_in = self._line_in_state()
        if line_in is not None:
            # A line input carries no text of its own: only the player feeding
            # it knows, and the amplifier's text is left over from earlier.
            title = line_in.attributes.get("media_title")
            if title and title.casefold() not in {"home assistant", "music assistant"}:
                return title
            extra = self._media_extras.get(self._last_media_id, {})
            return extra.get("title") or None

        amp_text = self.coordinator.data.text.get(self._zone) or ""
        renderer = self._dlna_state()

        if self._on_dlna and renderer:
            title = renderer.attributes.get("media_title")
            if title and title.casefold() not in {"home assistant", "music assistant"}:
                return title
            extra = self._media_extras.get(self._last_media_id, {})
            if extra.get("title"):
                return extra["title"]

        # Queue knows what it is playing.
        if self._queue and 0 <= self._queue_index < len(self._queue):
            item = self._queue[self._queue_index]
            if "|" in item:
                return item.split("|", 1)[1].lstrip("._")

        if amp_text and amp_text != "Home Assistant":
            return amp_text

        return None

    @property
    def media_artist(self) -> str | None:
        renderer = self._metadata_state()
        return renderer.attributes.get("media_artist") if renderer else None

    @property
    def media_duration(self) -> int | None:
        # This entity is MA's output device: its timing must describe the
        # renderer stream, never MA's derived per-track timeline.
        renderer = self._linked_state()
        return renderer.attributes.get("media_duration") if renderer else None

    def _playback_timing(self):
        """Report only the physical renderer's original stream timing."""
        player = self._linked_state()
        if player is None:
            return None, None
        return _track_timing(player, dt_util.utcnow())

    @property
    def media_position(self) -> float | None:
        return self._playback_timing()[0]

    @property
    def media_position_updated_at(self):
        return self._playback_timing()[1]

    @property
    def extra_state_attributes(self) -> dict[str, int | str] | None:
        if self._state is None:
            return None
        attrs: dict[str, int | str] = {
            "zone": self._zone,
            "source_value": self._state.source,
            "treble": self._state.treble,
            "bass": self._state.bass,
        }
        if self._queue:
            attrs["queue_size"] = len(self._queue)
            attrs["queue_position"] = self._queue_index + 1
        return attrs

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Seeking is only offered when a renderer is driving this zone.

        The amplifier's own player has no seek command on the network
        protocol, so advertising a progress bar it cannot honour would only
        mislead.
        """
        features = FEATURES
        player = self._metadata_state()
        if (
            player and player.attributes.get("media_duration")
            and int(player.attributes.get("supported_features", 0))
            & MediaPlayerEntityFeature.SEEK
        ):
            features |= MediaPlayerEntityFeature.SEEK
        return features

    async def async_media_seek(self, position: float) -> None:
        """Jump to a position in the current track, via the linked renderer."""
        if not math.isfinite(position) or position < 0:
            raise HomeAssistantError("Seek position must be a non-negative number")
        player = self._metadata_state()
        if not player or not (
            int(player.attributes.get("supported_features", 0))
            & MediaPlayerEntityFeature.SEEK
        ):
            raise HomeAssistantError("The active player does not support seeking")
        duration = player.attributes.get("media_duration")
        if duration:
            position = min(position, float(duration))
        music_assistant = self._music_assistant_state()
        if music_assistant:
            # Track seconds belong to MA's queue, not to the long-lived DLNA
            # stream. Never forward them directly to the renderer.
            if getattr(self, "_seeking_music_assistant", False):
                raise HomeAssistantError("Music Assistant routed seek back to its output")
            self._seeking_music_assistant = True
            try:
                await self.hass.services.async_call(
                    "media_player", "media_seek",
                    {"entity_id": music_assistant.entity_id, "seek_position": position},
                    blocking=True,
                )
            finally:
                self._seeking_music_assistant = False
            return
        if self._line_in_state() is not None:
            await self._async_line_in_service("media_seek", seek_position=position)
            return
        await self._async_dlna_service("media_seek", seek_position=position)

    @property
    def _group_zones(self) -> list[int]:
        """Zones explicitly joined together by the user."""
        return self.coordinator.group_of(self._zone)

    @property
    def group_members(self) -> list[str] | None:
        """Entity ids of the zones playing together with this one."""
        members = self._group_zones
        if len(members) < 2:
            return None
        entities = self.coordinator.zone_entities
        return [entities[z] for z in members if z in entities]

    async def async_join_players(self, group_members: list[str]) -> None:
        """Group other zones with this one, starting audio immediately."""
        entities = self.coordinator.zone_entities
        by_entity = {entity: zone for zone, entity in entities.items()}
        existing = list(self._group_zones)
        added = list(dict.fromkeys(
            by_entity[e] for e in group_members
            if e in by_entity and by_entity[e] not in existing
        ))
        if not added:
            return

        leader = existing[0]
        leader_state = self.coordinator.data.zones.get(leader)
        if leader_state and is_dlna_source(leader_state.source):
            await self._async_join_dlna(existing, added)
            return

        existing_renderers = set(self._group_renderers())
        self.coordinator.set_group(existing[0], existing + added)
        if self._on_dlna:
            await self._async_park_siblings()

        # Every joined zone: power on and match the leader's source.
        leader_source = self._state.source if self._state else SOURCE_DLNA
        for zone in added:
            state = self.coordinator.data.zones.get(zone)
            if state is None:
                continue
            if not state.power:
                state.power = True
                try:
                    await self.coordinator.hub.async_set_power(zone, True)
                except Am8328Error as err:
                    _LOGGER.warning("Could not power on zone %s: %s", zone, err)
                    continue

            if state.source != leader_source:
                state.source = leader_source
                try:
                    await self.coordinator.hub.async_set_source(zone, leader_source)
                except Am8328Error as err:
                    _LOGGER.warning("Could not set source for zone %s: %s", zone, err)

        # For internet radio, step the joined zone's station to match the
        # leader. The amp keeps per-zone station positions, so a zone that
        # just switched to radio lands on whatever it had last. Stepping it
        # to the leader's station is the best we can do without a "go to
        # station N" command.
        if leader_source & 0xF0 == 0xC0:
            leader_text = self.coordinator.data.text.get(self._zone) or ""
            if leader_text:
                for zone in added:
                    for _ in range(15):  # max stations to scan
                        zone_text = await self.coordinator.hub.async_get_text(zone)
                        if zone_text == leader_text:
                            break
                        try:
                            await self.coordinator.hub.async_track_step(zone, STEP_NEXT)
                        except Am8328Error:
                            break
                        await asyncio.sleep(0.8)

        self.coordinator.apply_optimistic()

        # Existing renderers already feed their newly joined zones. Start
        # only new renderers, without restarting the rooms already playing.
        current = self._current_media() if self._on_dlna else None
        if current is None:
            _LOGGER.debug("Zone %s has nothing to replay", self._zone)
            return

        media_type, media_id = current
        started = existing_renderers

        for zone in added:
            entity = self._renderer_for_zone(zone)
            if not entity or entity in started:
                continue
            started.add(entity)
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": entity,
                        "media_content_type": media_type,
                        "media_content_id": media_id,
                        "extra": dict(self._media_extras.get(media_id, {})),
                    },
                    blocking=True,
                )
                _LOGGER.debug("Started media on %s for zone %s", entity, zone)
            except Exception as err:
                _LOGGER.error("Failed to start media on %s: %s", entity, err)

    def _group_stream(self, leader: int):
        """Use the leader's live transport URI before any local queue cache."""
        renderer_id = self._renderer_for_zone(leader)
        renderer = self.hass.states.get(renderer_id) if renderer_id else None
        if renderer and renderer.state in ("playing", "paused"):
            uri = renderer.attributes.get("media_content_id")
            if uri and uri.startswith(("http://", "https://")):
                return renderer.attributes.get("media_content_type") or "music", uri
        # The record is shared across zones, so joining via a follower does
        # not accidentally replay that follower's previous independent queue.
        last = self.coordinator.renderer_started.get(renderer_id)
        if last:
            return "music", last[0]
        if leader == self._zone:
            return self._current_media()
        return None

    async def _async_join_dlna(self, existing: list[int], added: list[int]) -> None:
        """Join distinct renderers without hiding failures or restarting the leader."""
        leader = existing[0]
        current = self._group_stream(leader)
        old_renderers = {self._renderer_for_zone(z) for z in existing}
        new_renderers = list(dict.fromkeys(
            self._renderer_for_zone(z) for z in added
            if self._renderer_for_zone(z) not in old_renderers
        ))
        for zone in added:
            entity_id = self._renderer_for_zone(zone)
            target = self.hass.states.get(entity_id) if entity_id else None
            if not entity_id or target is None or target.state in ("unavailable", "unknown"):
                raise HomeAssistantError(
                    f"Cannot join zone {zone}: DLNA renderer {entity_id} is unavailable. "
                    "Check this zone's renderer setting."
                )
        if new_renderers and current is None:
            raise HomeAssistantError(
                "Cannot join a separate renderer: the group leader has no current "
                "stream URL. Start playback on the leader, then join the zone."
            )

        async with self.coordinator.control_lock:
            saved = {
                z: (self.coordinator.data.zones[z].power,
                    self.coordinator.data.zones[z].source)
                for z in added if z in self.coordinator.data.zones
            }
            try:
                # Do not publish a successful group while hardware routing failed.
                for attempt in range(3):
                    for zone in added:
                        await self.coordinator.hub.async_set_power(zone, True)
                        await self.coordinator.hub.async_set_source(zone, SOURCE_DLNA)
                    await asyncio.sleep(0.2)
                    actual = await self.coordinator.hub.async_get_state()
                    if all(z in actual and actual[z].power and
                           is_dlna_source(actual[z].source) for z in added):
                        break
                else:
                    raise HomeAssistantError(
                        f"Cannot join zones {added}: amplifier did not confirm power/input"
                    )

                if current:
                    media_type, media_id = current
                    for renderer_id in new_renderers:
                        try:
                            await self.hass.services.async_call(
                                "media_player", "play_media",
                                {"entity_id": renderer_id,
                                 "media_content_type": media_type,
                                 "media_content_id": media_id,
                                 "extra": dict(self._media_extras.get(media_id, {}))},
                                blocking=True,
                            )
                        except Exception as err:
                            raise HomeAssistantError(
                                f"Cannot join: renderer {renderer_id} rejected playback: {err}"
                            ) from err
                        # A service returning successfully only means the command
                        # was sent. Ask HA's renderer to report whether it started.
                        for _ in range(3):
                            await asyncio.sleep(1)
                            await self.hass.services.async_call(
                                "homeassistant", "update_entity",
                                {"entity_id": renderer_id}, blocking=True,
                            )
                            reported = self.hass.states.get(renderer_id)
                            if reported and reported.state == "playing":
                                break
                        else:
                            raise HomeAssistantError(
                                f"Renderer {renderer_id} accepted the command but did not "
                                "report playing. Check DLNA and Music Assistant logs."
                            )
                        self.coordinator.renderer_started[renderer_id] = (
                            media_id, time.monotonic()
                        )
                for zone in added:
                    if zone in self.coordinator.data.zones:
                        self.coordinator.data.zones[zone].power = True
                        self.coordinator.data.zones[zone].source = actual[zone].source
                self.coordinator.set_group(leader, existing + added)
            except Exception:
                for zone, (power, source) in saved.items():
                    try:
                        await self.coordinator.hub.async_set_source(zone, source)
                        await self.coordinator.hub.async_set_power(zone, power)
                    except Exception as err:
                        _LOGGER.warning("Could not restore zone %s after failed join: %s", zone, err)
                raise
        await self._async_park_siblings()
        _LOGGER.debug(
            "Joined zones %s to leader %s using additional renderers %s",
            added, leader, new_renderers,
        )

    def _current_media(self) -> tuple[str, str] | None:
        """What this zone is playing, as (type, id).

        Uses the stored last-played info rather than requiring the state to
        read PLAYING, because polling lag means the state often reads ON or
        IDLE at the moment join is called.
        """
        if self._queue and 0 <= self._queue_index < len(self._queue):
            return (self._queue_type, self._queue[self._queue_index])
        if self._last_media_id:
            return (self._last_media_type, self._last_media_id)
        renderer = self._dlna_state()
        if renderer:
            content_id = renderer.attributes.get("media_content_id")
            if content_id:
                return (
                    renderer.attributes.get("media_content_type", "audio/mpeg"),
                    content_id,
                )
        return None

    async def async_unjoin_player(self) -> None:
        """Leave the group and stop hearing its audio.

        Several zones can share one renderer in hardware, so simply dropping
        out of the group would leave the room still playing. The zone is moved
        to a line input instead - configurable in the options - which silences
        the shared programme while leaving the zone powered and usable.
        """
        async with self.coordinator.control_lock:
            leave_source = int(
                self._entry.options.get(CONF_LEAVE_SOURCE, DEFAULT_LEAVE_SOURCE)
            )
            if is_dlna_source(leave_source):
                raise HomeAssistantError("The leave source must be AUX, not Internet music")
            # Confirm physical isolation before publishing the membership change.
            # A write acknowledgement alone does not prove the input changed.
            for attempt in range(3):
                await self.coordinator.hub.async_set_source(self._zone, leave_source)
                await asyncio.sleep(0.2)
                actual = await self.coordinator.hub.async_get_state()
                state = actual.get(self._zone)
                if state is not None and state.source == leave_source:
                    break
            else:
                raise HomeAssistantError(
                    f"Zone {self._zone} did not switch to the leave source; "
                    "group membership was kept. Check the amplifier connection."
                )
            if self._state:
                self._state.source = leave_source
            self._queue = []
            self._queue_index = 0
            self.coordinator.recent_play.pop(self._zone, None)
            self.coordinator.clear_group(self._zone)
            # Do not stop the renderer: the zone is isolated by its input and
            # another room may still need the same physical stream.
            self.coordinator.apply_optimistic()

    @property
    def _dlna_entity(self) -> str | None:
        """The DLNA renderer linked to this zone, if any."""
        return self._renderer_for_zone(self._zone)

    def _renderer_for_zone(self, zone: int) -> str | None:
        """Resolve explicit and legacy renderer settings consistently."""
        if zone in LINE_IN_PLAYERS:
            # Fed by a line input: this zone does not use a DLNA renderer.
            return None
        mapping = self._entry.options.get(CONF_DLNA_ENTITIES) or {}
        return mapping.get(str(zone)) or self._entry.options.get(CONF_DLNA_ENTITY)

    @property
    def _airplay_entity(self) -> str | None:
        """The AirPlay player linked to this zone, if any."""
        mapping = self._entry.options.get(CONF_AIRPLAY_ENTITIES) or {}
        return mapping.get(str(self._zone))

    @property
    def _on_dlna(self) -> bool:
        return bool(self._state and is_dlna_source(self._state.source))

    def _dlna_state(self):
        """The linked renderer's state object, when this zone is using it."""
        entity = self._dlna_entity
        if not entity or not self._on_dlna:
            return None
        return self.hass.states.get(entity)

    # -- line input ---------------------------------------------------------

    @property
    def _line_in_entity(self) -> str | None:
        """The player wired into this zone's line input, if any."""
        config = LINE_IN_PLAYERS.get(self._zone)
        return config[1] if config else None

    @property
    def _line_in_source(self) -> int | None:
        """The amplifier source value of this zone's line input."""
        config = LINE_IN_PLAYERS.get(self._zone)
        if not config:
            return None
        source = config[0]
        return source if isinstance(source, int) else self._resolve_source(source)

    @property
    def _on_line_in(self) -> bool:
        source = self._line_in_source
        return bool(
            self._state and source is not None and self._state.source == source
        )

    def _line_in_state(self):
        """The line-in player's state object, when this zone is listening to it."""
        entity = self._line_in_entity
        if not entity or not self._on_line_in:
            return None
        return self.hass.states.get(entity)

    def _linked_state(self):
        """The external player feeding this zone right now: line-in or DLNA."""
        return self._line_in_state() or self._dlna_state()

    async def _async_line_in_service(self, service: str, **data) -> None:
        """Send a media_player service to this zone's line-in player."""
        entity = self._line_in_entity
        if not entity:
            return
        await self.hass.services.async_call(
            "media_player", service, {"entity_id": entity, **data}, blocking=True
        )

    async def _async_restore_volume(self) -> None:
        """Re-apply the zone's volume after a renderer may have reset it."""
        async with self.coordinator.control_lock:
            state = self._state
            if state is None:
                return
            try:
                await self.coordinator.hub.async_set_volume(self._zone, state.volume)
            except Am8328Error:
                pass

    def _group_renderers(self) -> list[str]:
        """Every distinct renderer serving this zone's group."""
        renderers: list[str] = []
        for zone in self._group_zones:
            entity = self._renderer_for_zone(zone)
            if entity and entity not in renderers:
                renderers.append(entity)
        return renderers

    async def _async_dlna_service(self, service: str, **data) -> None:
        """Send a media_player service to every renderer in the group."""
        for entity in self._group_renderers() or [self._dlna_entity]:
            if not entity:
                continue
            await self.hass.services.async_call(
                "media_player", service, {"entity_id": entity, **data}, blocking=True
            )

    # -- commands -----------------------------------------------------------

    def _resolve_source(self, name: str) -> int | None:
        for value, alias in self.coordinator.data.aliases.items():
            if alias == name:
                return value
        for value, label in NETWORK_SOURCES.items():
            if label == name:
                return value
        return None

    async def _async_run(self, coro, description: str) -> None:
        try:
            await coro
        except Am8328Error as err:
            raise HomeAssistantError(f"AM8328 {description} failed: {err}") from err
        self.coordinator.apply_optimistic()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self._async_set_power(True, "turn on")

    async def async_turn_off(self) -> None:
        await self._async_set_power(False, "turn off")

    async def _async_set_power(self, on: bool, description: str) -> None:
        """Power this zone only.

        The amplifier switches each zone's output separately, so turning off
        one room must not silence the others - even when they share a renderer
        and are shown as a group.
        """
        if self._state:
            self._state.power = on
        await self._async_run(
            self.coordinator.hub.async_set_power(self._zone, on), description
        )

    async def async_set_volume_level(self, volume: float) -> None:
        level = round(volume * MAX_VOLUME)
        await self._async_apply_volume(level)

    async def _async_apply_volume(self, level: int) -> None:
        """Set this zone and every zone grouped with it to the same level."""
        if self._state is None:
            return
        level = max(0, min(level, MAX_VOLUME))
        async with self.coordinator.control_lock:
            members = list(self._group_zones)
            pending = list(members)
            for attempt in range(3):
                for zone in pending:
                    await self.coordinator.hub.async_set_volume(zone, level)
                await asyncio.sleep(0.2)
                actual = await self.coordinator.hub.async_get_state()
                pending = [
                    zone for zone in members
                    if zone not in actual or actual[zone].volume != level
                ]
                for zone in members:
                    if zone in actual and zone in self.coordinator.data.zones:
                        self.coordinator.data.zones[zone].volume = actual[zone].volume
                if not pending:
                    break
            self.coordinator.apply_optimistic()
            if pending:
                raise HomeAssistantError(
                    f"Volume was not confirmed for zones {pending}; requested {level}%"
                )

    async def async_volume_up(self) -> None:
        if self._state is None:
            return
        await self._async_apply_volume(self._state.volume + VOLUME_STEP)

    async def async_volume_down(self) -> None:
        if self._state is None:
            return
        await self._async_apply_volume(self._state.volume - VOLUME_STEP)

    async def async_select_source(self, source: str) -> None:
        value = self._resolve_source(source)
        if value is None:
            raise HomeAssistantError(f"Unknown source: {source}")
        for zone in self._group_zones:
            state = self.coordinator.data.zones.get(zone)
            if state is not None:
                state.source = value
            await self._async_run(
                self.coordinator.hub.async_set_source(zone, value), "select source"
            )

    # -- transport ----------------------------------------------------------

    async def async_media_play(self) -> None:
        await self._async_playback(PLAY, "play")

    async def async_media_pause(self) -> None:
        await self._async_playback(PAUSE, "pause")

    async def async_media_stop(self) -> None:
        # Stop means stop - drop the queue so it does not roll on.
        self._queue = []
        self._queue_index = 0
        await self._async_playback(STOP, "stop")

    async def _async_playback(self, state: int, description: str) -> None:
        # When the zone is listening to its own DLNA renderer, transport has to
        # go to that renderer - the amplifier's play/pause drives its internal
        # player, not the UPnP one. The same goes for a line-in player.
        if self._line_in_entity and self._on_line_in:
            if state == PLAY:
                self._line_in_play_at = time.monotonic()
            await self._async_line_in_service(
                {PLAY: "media_play", PAUSE: "media_pause", STOP: "media_stop"}[state]
            )
            return
        if self._dlna_entity and self._on_dlna:
            await self._async_dlna_service(
                {PLAY: "media_play", PAUSE: "media_pause", STOP: "media_stop"}[state]
            )
            return
        for zone in self._group_zones:
            self.coordinator.data.play_state[zone] = state
            await self._async_run(
                self.coordinator.hub.async_set_play_state(zone, state), description
            )

    async def async_media_next_track(self) -> None:
        if self._has_queue:
            await self._async_advance(1)
            return
        if self._line_in_entity and self._on_line_in:
            await self._async_line_in_service("media_next_track")
            return
        if self._dlna_entity and self._on_dlna:
            await self._async_dlna_service("media_next_track")
            return
        for zone in self._group_zones:
            await self._async_run(
                self.coordinator.hub.async_track_step(zone, STEP_NEXT), "next track"
            )

    async def async_media_previous_track(self) -> None:
        if self._has_queue:
            await self._async_advance(-1)
            return
        if self._line_in_entity and self._on_line_in:
            await self._async_line_in_service("media_previous_track")
            return
        if self._dlna_entity and self._on_dlna:
            await self._async_dlna_service("media_previous_track")
            return
        for zone in self._group_zones:
            await self._async_run(
                self.coordinator.hub.async_track_step(zone, STEP_PREVIOUS),
                "previous track",
            )

    # -- media browsing -----------------------------------------------------

    async def async_browse_media(
        self, media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the amplifier's SD/USB card and Home Assistant's own media."""
        if media_content_id and media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=_is_playable
            )

        if media_content_id and media_content_id.startswith(FOLDER_PREFIX):
            return await self._async_browse_folder(
                media_content_id[len(FOLDER_PREFIX) :]
            )

        if media_content_id == SDUSB_ROOT:
            return self._sdusb_root(expanded=True)

        # Top level: the card, plus everything Home Assistant can serve.
        children: list[BrowseMedia] = [self._sdusb_root(expanded=False)]
        try:
            ha_media = await media_source.async_browse_media(
                self.hass, None, content_filter=_is_playable
            )
        except BrowseError:
            ha_media = None
        if ha_media and ha_media.children:
            children.extend(ha_media.children)

        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="root",
            media_content_type=MediaType.MUSIC,
            title=self.name or "Zone",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    def _sdusb_root(self, *, expanded: bool) -> BrowseMedia:
        """The amplifier's own card, listing its folders."""
        folders = self.coordinator.data.folders
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=SDUSB_ROOT,
            media_content_type=MediaType.MUSIC,
            title="SD / USB",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=f"{FOLDER_PREFIX}{name}",
                    media_content_type=MediaType.MUSIC,
                    title=name,
                    # Playing a folder queues everything inside it.
                    can_play=True,
                    can_expand=True,
                )
                for name in folders.values()
            ]
            if expanded
            else [],
        )

    async def _async_browse_folder(self, name: str) -> BrowseMedia:
        folders = self.coordinator.data.folders
        index = next((i for i, n in folders.items() if n == name), None)
        tracks: list[str] = []
        if index is not None:
            try:
                tracks = await self.coordinator.hub.async_get_tracks(index, name)
            except Am8328Error as err:
                raise HomeAssistantError(f"Cannot list {name}: {err}") from err
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=f"{FOLDER_PREFIX}{name}",
            media_content_type=MediaType.MUSIC,
            title=name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.TRACK,
            children=[
                BrowseMedia(
                    media_class=MediaClass.TRACK,
                    media_content_id=f"{name}|{track}",
                    media_content_type=MediaType.MUSIC,
                    title=track.lstrip("._"),
                    can_play=True,
                    can_expand=False,
                )
                for track in tracks
            ],
        )

    # -- queue --------------------------------------------------------------

    async def _async_expand(self, media_type: str, media_id: str) -> list[str]:
        """Turn a folder into the list of tracks inside it.

        A single track expands to itself, so callers do not need to care which
        they were given.
        """
        # A folder on the amplifier's own card.
        if media_id.startswith(FOLDER_PREFIX):
            name = media_id[len(FOLDER_PREFIX) :]
            folders = self.coordinator.data.folders
            index = next((i for i, n in folders.items() if n == name), None)
            if index is None:
                return []
            tracks = await self.coordinator.hub.async_get_tracks(index, name)
            return [f"{name}|{track}" for track in tracks]

        # An M3U playlist: expand it into the tracks it lists.
        resolved = media_id
        if media_source.is_media_source_id(media_id):
            lowered = media_id.lower()
            if lowered.endswith((".m3u", ".m3u8")):
                item = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                resolved = async_process_play_media_url(self.hass, item.url)
        if resolved.lower().split("?")[0].endswith((".m3u", ".m3u8")):
            entries = await self._async_read_playlist(resolved)
            if entries:
                return entries

        # A directory served by Home Assistant. Anything that is not a
        # directory - a radio station, a single file - browses badly or not at
        # all, so any failure here simply means "play this one thing".
        if media_source.is_media_source_id(media_id):
            try:
                browsed = await media_source.async_browse_media(
                    self.hass, media_id, content_filter=_is_playable
                )
            except Exception as err:  # noqa: BLE001 - never block playback
                _LOGGER.debug("Not expandable, playing directly: %s (%s)", media_id, err)
                return [media_id]
            if browsed.children:
                children = [
                    child.media_content_id
                    for child in browsed.children
                    if child.can_play
                ]
                if children:
                    return children

        return [media_id]

    async def _async_read_playlist(self, url: str) -> list[str]:
        """Read an M3U playlist and return the entries it points at.

        Relative paths are resolved against the playlist's own location, so a
        file listing plain track names alongside itself works.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                body = await r.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not read playlist %s: %s", url, err)
            return []

        entries: list[str] = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(urljoin(url, line))
        return entries

    async def _async_advance(self, step: int = 1) -> None:
        """Play the next (or previous) item, honouring repeat and shuffle."""
        if not self._queue:
            return

        if self._attr_repeat == RepeatMode.ONE and step > 0:
            await self._async_play_item(
                self._queue_type, self._queue[self._queue_index]
            )
            return

        index = self._queue_index + step
        if not 0 <= index < len(self._queue):
            if self._attr_repeat == RepeatMode.ALL:
                if self._attr_shuffle:
                    random.shuffle(self._queue)
                index %= len(self._queue)
            else:
                self._queue = []
                self._queue_index = 0
                return

        self._queue_index = index
        await self._async_play_item(self._queue_type, self._queue[index])

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Off, repeat one, or loop the whole list."""
        self._attr_repeat = repeat
        self.async_write_ha_state()

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Shuffle the pending queue, keeping the current track in place."""
        self._attr_shuffle = shuffle
        if shuffle and len(self._queue) > 1:
            current = self._queue[self._queue_index]
            rest = [
                item
                for i, item in enumerate(self._queue)
                if i != self._queue_index
            ]
            random.shuffle(rest)
            self._queue = [current] + rest
            self._queue_index = 0
        self.async_write_ha_state()

    @property
    def _has_queue(self) -> bool:
        return len(self._queue) > 1

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Play, enqueue, or announce media.

        Supports Music Assistant's enqueue and announce parameters:
        - announce=True: duck the volume, play the media, restore volume
        - enqueue="add": append to the current queue
        - enqueue="next": insert after the current track
        - enqueue="play" or default: replace the queue and start
        """
        announce = kwargs.get("announce", False)
        enqueue = kwargs.get("enqueue")
        self.coordinator.recent_play[self._zone] = time.monotonic()

        if announce:
            await self._async_announce(media_type, media_id, **kwargs)
            return

        items = await self._async_expand(media_type, media_id)
        _LOGGER.debug("Zone %s expanded %s into %d item(s) (enqueue=%s)",
                      self._zone, media_id, len(items), enqueue)
        if not items:
            raise HomeAssistantError(f"Nothing playable in {media_id}")

        if enqueue not in ("add", "next") or not self._queue:
            self._media_extras.clear()
        if len(items) == 1:
            self._media_extras[items[0]] = _normalize_media_extra(kwargs.get("extra"))

        if enqueue == "add" and self._queue:
            # Append to the end of the current queue.
            self._queue.extend(items)
            return

        if enqueue == "next" and self._queue:
            # Insert right after the currently playing track.
            insert_at = self._queue_index + 1
            for i, item in enumerate(items):
                self._queue.insert(insert_at + i, item)
            return

        # Default / enqueue="play": replace the queue and start.
        self._queue = items
        self._queue_type = media_type
        self._queue_index = 0
        self._last_media_type = media_type
        self._last_media_id = media_id
        await self._async_play_item(media_type, items[0])

    async def _async_announce(self, media_type: str, media_id: str, **kwargs) -> None:
        """Play an announcement, then restore volume.

        Ducks the volume to ANNOUNCE_VOLUME, plays the media, waits for it
        to finish, then restores the original volume. Used by Music Assistant
        and Home Assistant's TTS announce feature.
        """
        state = self._state
        if state is None:
            return

        # Save and duck volume
        saved_volume = state.volume
        announce_level = round(ANNOUNCE_VOLUME * MAX_VOLUME)
        _LOGGER.debug(
            "Zone %s announce: ducking volume %d -> %d",
            self._zone, saved_volume, announce_level,
        )

        line_in_source = self._line_in_source
        await self._async_wake(
            SOURCE_DLNA if line_in_source is None else line_in_source
        )

        try:
            await self.coordinator.hub.async_set_volume(self._zone, announce_level)
        except Am8328Error:
            pass

        # Play the announcement
        try:
            await self._async_play_item(media_type, media_id, **kwargs)
        except Exception as err:
            # Restore volume even if playback fails
            try:
                await self.coordinator.hub.async_set_volume(self._zone, saved_volume)
            except Am8328Error:
                pass
            raise

        # Wait for it to finish by watching the linked player
        renderer = self._linked_state()
        if renderer:
            for _ in range(120):  # max 2 minutes
                await asyncio.sleep(1)
                renderer = self._linked_state()
                if not renderer or renderer.state != MediaPlayerState.PLAYING:
                    break
        else:
            # No renderer — wait a fixed period for the amplifier's own player
            await asyncio.sleep(5)

        # Restore volume
        _LOGGER.debug("Zone %s announce done, restoring volume to %d",
                      self._zone, saved_volume)
        try:
            await self.coordinator.hub.async_set_volume(self._zone, saved_volume)
            if state:
                state.volume = saved_volume
            self.coordinator.apply_optimistic()
        except Am8328Error:
            pass

    async def _async_play_item(self, media_type: str, media_id: str, **kwargs) -> None:
        """Play exactly one item, without touching the queue."""
        kwargs["extra"] = _normalize_media_extra(
            kwargs.get("extra", self._media_extras.get(media_id, {}))
        )
        self._last_media_type = media_type
        self._last_media_id = media_id
        # A track on the amplifier's own card always uses the native command.
        if "|" in media_id and not media_id.startswith("http"):
            folder, track = media_id.split("|", 1)
            await self._async_wake(SOURCE_SDUSB)
            await self._async_run(
                self.coordinator.hub.async_play_file(folder, track), "play media"
            )
            return

        line_in = self._line_in_entity
        if line_in:
            await self._async_play_via_line_in(line_in, media_type, media_id, **kwargs)
            return

        dlna_entity = self._dlna_entity
        if dlna_entity:
            await self._async_play_via_dlna(dlna_entity, media_type, media_id, **kwargs)
            return

        await self._async_play_via_stream(media_type, media_id, **kwargs)

    def _sibling_zones(self) -> list[int]:
        """Other zones sharing this zone's DLNA renderer in hardware."""
        if self._zone in LINE_IN_PLAYERS:
            return []
        mapping = self._entry.options.get(CONF_DLNA_ENTITIES) or {}
        my_renderer = mapping.get(str(self._zone)) or self._dlna_entity
        if not my_renderer:
            return []
        # Resolve every discovered zone exactly as _dlna_entity does. A zone
        # using the legacy fallback need not have an explicit mapping entry.
        legacy = self._entry.options.get(CONF_DLNA_ENTITY)
        return [
            zone for zone in self.coordinator.data.zones
            if zone != self._zone
            and zone not in LINE_IN_PLAYERS
            and (mapping.get(str(zone)) or legacy) == my_renderer
        ]

    async def _async_park_siblings(self) -> None:
        """Switch sibling zones to a silent source so only this zone is heard."""
        leave_source = int(
            self._entry.options.get(CONF_LEAVE_SOURCE, DEFAULT_LEAVE_SOURCE)
        )
        group_zones = set(self.coordinator.group_of(self._zone))

        now = time.monotonic()
        renderers = set(self._group_renderers())
        for zone in self.coordinator.data.zones:
            if self._renderer_for_zone(zone) not in renderers:
                continue
            if zone in group_zones:
                continue
            # Asked to play moments ago - part of a Music Assistant group
            # being started member by member. Leave it alone.
            if now - self.coordinator.recent_play.get(zone, float("-inf")) < RECENT_PLAY_WINDOW:
                continue
            state = self.coordinator.data.zones.get(zone)
            if state is None:
                continue
            self.coordinator.parked.add(zone)
            if state.source != leave_source:
                try:
                    await self.coordinator.hub.async_set_source(zone, leave_source)
                    state.source = leave_source
                except Am8328Error as err:
                    _LOGGER.warning("Could not park zone %s: %s", zone, err)

        self.coordinator.apply_optimistic()

    async def _async_play_via_line_in(
        self, player: str, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Switch the zone to its line input and start the player wired to it."""
        source = self._line_in_source
        if source is None:
            raise HomeAssistantError(
                f"Zone {self._zone}: line input "
                f"{LINE_IN_PLAYERS[self._zone][0]!r} is not one of the amplifier's "
                "sources. Check LINE_IN_PLAYERS in media_player.py."
            )
        target = self.hass.states.get(player)
        if target is None or target.state == "unavailable":
            raise HomeAssistantError(
                f"Line-in player {player} is unavailable. Check that it is running "
                "and that LINE_IN_PLAYERS in media_player.py names the right entity."
            )

        await self._async_wake(source)

        # Players such as VLC accept only music/audio types. Playlists and
        # folders were already expanded into single items before this point.
        if media_type != MediaType.MUSIC and not str(media_type).startswith("audio/"):
            media_type = MediaType.MUSIC
        data = {
            "entity_id": player,
            "media_content_type": media_type,
            # Passed through untouched: the player resolves media-source ids
            # and relative URLs itself.
            "media_content_id": media_id,
        }
        extra = _normalize_media_extra(kwargs.get("extra"))
        if extra:
            data["extra"] = extra
        _LOGGER.debug(
            "Zone %s playing %s (%s) on line-in player %s",
            self._zone, media_id, media_type, player,
        )
        previous = self._line_in_play_at
        self._line_in_play_at = time.monotonic()
        try:
            await self.hass.services.async_call(
                "media_player", "play_media", data, blocking=True
            )
        except Exception as err:
            self._line_in_play_at = previous
            raise HomeAssistantError(
                f"Line-in player {player} refused the media: {err}"
            ) from err

    async def _async_play_via_dlna(
        self, dlna_entity: str, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Wake the group onto DLNA, then start it on every renderer involved.

        Each zone has its own renderer, so playing to a group means sending
        the same media to each member's renderer - otherwise only the room you
        pressed play on makes any sound.
        """
        await self._async_park_siblings()
        await self._async_wake(SOURCE_DLNA)

        targets = self._group_renderers() or [dlna_entity]
        _LOGGER.debug(
            "Zone %s playing %s (%s) on %s",
            self._zone, media_id, media_type, ", ".join(targets),
        )
        # Zones sharing one renderer only need it started once.
        failures: list[str] = []
        for entity in targets:
            last = self.coordinator.renderer_started.get(entity)
            now = time.monotonic()
            if last and last[0] == media_id and now - last[1] < RENDERER_DEDUPE_WINDOW:
                # Another zone on this same renderer just started this media.
                continue
            self.coordinator.renderer_started[entity] = (media_id, now)
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": entity,
                        "media_content_type": media_type,
                        "media_content_id": media_id,
                        "extra": _normalize_media_extra(kwargs.get("extra")),
                    },
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001 - report, do not abort
                _LOGGER.error("Renderer %s refused the media: %s", entity, err)
                self.coordinator.renderer_started.pop(entity, None)
                failures.append(entity)

        if failures and len(failures) == len(targets):
            raise HomeAssistantError(
                f"No renderer accepted the media: {', '.join(failures)}"
            )

        await self._async_restore_volume()

    async def _async_play_via_stream(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Fallback: push a URL for the amplifier to fetch itself."""
        title = kwargs.get("extra", {}).get("title") or ""

        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = item.url
            if not title:
                title = getattr(item, "title", "") or ""
            if not title and getattr(item, "path", None):
                title = item.path.name

        if not title:
            # Last resort: extract something from the URL itself.
            from urllib.parse import unquote, urlparse
            path = urlparse(media_id).path
            title = unquote(path.rsplit("/", 1)[-1]).rsplit(".", 1)[0] or "Stream"

        url = async_process_play_media_url(self.hass, media_id)
        if len(url.encode("utf-8")) + len(title.encode("utf-8")) > MAX_STREAM_PAYLOAD:
            raise HomeAssistantError(
                "The media URL is too long for the amplifier's protocol. Link a "
                "DLNA player in the integration options, or use a shorter link."
            )
        if url.startswith("https://"):
            raise HomeAssistantError(
                "The amplifier cannot fetch https:// URLs. Link a DLNA player "
                "in the integration options, or use an http:// stream."
            )

        await self._async_wake(STREAM_SOURCE)
        await self._async_run(
            self.coordinator.hub.async_play_url(title, url), "play stream"
        )


def _is_playable(item: BrowseMedia) -> bool:
    """Offer audio, plus playlists that expand into audio."""
    if item.media_content_type.startswith("audio/"):
        return True
    return item.media_content_id.lower().endswith((".m3u", ".m3u8"))


def _normalize_media_extra(extra: dict | None) -> dict:
    """Translate Music Assistant/Cast metadata to HA DLNA's title field."""
    result = dict(extra or {})
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        for key in ("title", "artist", "album", "duration"):
            if metadata.get(key) and not result.get(key):
                result[key] = metadata[key]
        if metadata.get("imageUrl") and not result.get("thumb"):
            result["thumb"] = metadata["imageUrl"]
    return result


def _track_timing(player, now: datetime):
    """Preserve the renderer's position/anchor pair without re-anchoring it.

    MA consumes output elapsed time to derive its queue position. Feeding its
    own track position back, or clamping stream seconds to a song duration,
    corrupts that calculation. Missing anchors must remain unknown.
    """
    position = player.attributes.get("media_position")
    if not isinstance(position, (int, float)) or not math.isfinite(position):
        return None, None
    stamp = player.attributes.get("media_position_updated_at")
    if isinstance(stamp, str):
        stamp = dt_util.parse_datetime(stamp)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        return None, None
    return max(0, position), stamp
