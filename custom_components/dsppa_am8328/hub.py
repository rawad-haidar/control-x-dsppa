"""UDP transport and commands for a DSPPA AM8328."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable

from .const import (
    CMD_FOLDERS,
    CMD_INFO,
    CMD_NOW_PLAYING,
    CMD_PLAY_FILE,
    CMD_PLAY_URL,
    CMD_PLAY_STATE,
    CMD_TONE,
    CMD_TRACKS,
    CMD_TRACK_STEP,
    CMD_POWER,
    CMD_SOURCE,
    CMD_SOURCE_LIST,
    CMD_STATE,
    CMD_TEXT,
    CMD_VOLUME,
    CMD_ZONE_NAMES,
    LISTEN_PORT,
    MAX_VOLUME,
    READ,
    RESPONSE_TIMEOUT,
    TARGET_ALL,
    TARGET_SYSTEM,
    WRITE,
    WRITE_ATTEMPTS,
)
from .protocol import (
    Frame,
    parse_now_playing,
    build_play_file,
    build_play_url,
    parse_folders,
    parse_tracks,
    ZoneState,
    build_frame,
    parse_frame,
    parse_info,
    parse_named_list,
    parse_source_list,
    parse_state,
)

_LOGGER = logging.getLogger(__name__)


class Am8328Error(Exception):
    """A command failed or the amplifier did not answer."""


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: "Am8328Hub") -> None:
        self._hub = hub

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] != self._hub.host:
            return
        frame = parse_frame(data)
        if frame is not None:
            self._hub.handle_frame(frame)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("AM8328 socket error: %s", exc)


class Am8328Hub:
    """Talks to the amplifier over UDP.

    The device replies to the port we send from, so we bind the same port the
    official app uses. Replies are matched on (command, sequence).
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._waiters: dict[tuple[int, int], deque[asyncio.Future[Frame]]] = (
            defaultdict(deque)
        )
        self._seq = 1
        self._lock = asyncio.Lock()
        self._on_now_playing: Callable[[int, str, int], None] | None = None

    def set_now_playing_callback(
        self, callback: "Callable[[int, str, int], None] | None"
    ) -> None:
        """Register a callback for pushed programme changes."""
        self._on_now_playing = callback

    # -- lifecycle ----------------------------------------------------------

    async def async_connect(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self),
                local_addr=("0.0.0.0", LISTEN_PORT),
                allow_broadcast=True,
                reuse_port=True,
            )
        except OSError as err:
            raise Am8328Error(
                f"cannot bind UDP {LISTEN_PORT}: {err}. Another program on this "
                "host may be using it."
            ) from err

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    def handle_frame(self, frame: Frame) -> None:
        queue = self._waiters.get((frame.cmd, frame.seq))
        if queue:
            future = queue.popleft()
            if not future.done():
                future.set_result(frame)
            return

        # Unsolicited: the amplifier pushes 0x0025 whenever a zone's programme
        # changes, so the UI can update without waiting for the next poll.
        if frame.cmd == CMD_NOW_PLAYING and self._on_now_playing:
            parsed = parse_now_playing(frame.data)
            if parsed:
                self._on_now_playing(*parsed)

    # -- request/response ---------------------------------------------------

    async def async_write(
        self, cmd: int, target: int, data: bytes,
        *, attempts: int = WRITE_ATTEMPTS,
    ) -> bool:
        """Send a write command, repeating it until it is acknowledged.

        The amplifier drops acknowledgements fairly often - the official app
        works around this by sending each command twice. Returns True if the
        device acknowledged; the caller can still trust the next state poll,
        because commands usually take effect even when the reply is lost.
        """
        for attempt in range(attempts):
            if await self.async_send(cmd, target, data) is not None:
                return True
            _LOGGER.debug(
                "No ack for %s (attempt %d/%d)", f"{cmd:#06x}", attempt + 1, attempts
            )
        return False

    async def async_send(
        self, cmd: int, target: int = TARGET_SYSTEM, data: bytes = b"",
        *, use_seq: bool = True,
    ) -> bytes | None:
        """Send a frame and return the reply payload, or None on timeout."""
        if self._transport is None:
            raise Am8328Error("not connected")

        async with self._lock:
            if use_seq:
                seq = self._seq
                self._seq = (self._seq + 1) & 0xFF or 1
            else:
                seq = 0

            loop = asyncio.get_running_loop()
            future: asyncio.Future[Frame] = loop.create_future()
            key = (cmd, seq)
            self._waiters[key].append(future)

            frame = build_frame(cmd, target, data, seq)
            _LOGGER.debug("TX %s", frame.hex(" "))
            self._transport.sendto(frame, (self.host, self.port))

            try:
                reply = await asyncio.wait_for(future, RESPONSE_TIMEOUT)
            except asyncio.TimeoutError:
                if future in self._waiters[key]:
                    self._waiters[key].remove(future)
                return None
            return reply.data

    # -- reads --------------------------------------------------------------

    async def async_get_info(self) -> list[str]:
        data = await self.async_send(CMD_INFO, TARGET_SYSTEM, b"", use_seq=False)
        return parse_info(data) if data else []

    async def async_get_state(self) -> dict[int, ZoneState]:
        """One frame carrying source, volume, power and tone for every zone."""
        data = await self.async_send(CMD_STATE, TARGET_ALL, b"", use_seq=False)
        if data is None:
            raise Am8328Error("no reply to state query")
        return parse_state(data)

    async def async_get_zone_names(self) -> dict[int, str]:
        data = await self.async_send(
            CMD_ZONE_NAMES, TARGET_ALL, bytes((READ,)), use_seq=False
        )
        return parse_named_list(data) if data else {}

    async def async_get_source_aliases(self) -> dict[int, str]:
        data = await self.async_send(
            CMD_SOURCE_LIST, TARGET_ALL, bytes((READ,)), use_seq=False
        )
        return parse_source_list(data) if data else {}

    async def async_get_text(self, zone: int) -> str:
        data = await self.async_send(CMD_TEXT, zone, b"", use_seq=False)
        return data.decode("utf-8", "replace") if data else ""

    # -- writes -------------------------------------------------------------

    async def async_set_volume(self, zone: int, level: int) -> None:
        level = max(0, min(int(level), MAX_VOLUME))
        await self.async_write(CMD_VOLUME, zone, bytes((WRITE, level)))

    async def async_set_source(self, zone: int, source: int) -> None:
        await self.async_write(CMD_SOURCE, zone, bytes((WRITE, source)))

    async def async_set_power(self, zone: int, on: bool) -> None:
        payload = bytes((WRITE, 0x01 if on else 0x00))
        await self.async_write(CMD_POWER, zone, payload)

    # -- playback -----------------------------------------------------------

    async def async_get_play_state(self, zone: int) -> int | None:
        """0x0005: 0x01 playing, 0x02 paused, 0x04 stopped."""
        data = await self.async_send(
            CMD_PLAY_STATE, zone, bytes((READ,)), use_seq=False
        )
        return data[0] if data else None

    async def async_set_play_state(self, zone: int, state: int) -> None:
        await self.async_write(CMD_PLAY_STATE, zone, bytes((WRITE, state)))

    async def async_track_step(self, zone: int, step: int) -> None:
        """0x000A: 0x10 next, 0x01 previous."""
        await self.async_write(CMD_TRACK_STEP, zone, bytes((step,)))

    async def async_set_tone(self, zone: int, treble: int, bass: int) -> None:
        """0x002F sets both tone controls in one frame."""
        payload = bytes((WRITE, treble & 0xFF, bass & 0xFF))
        await self.async_write(CMD_TONE, zone, payload)

    # -- media browsing -----------------------------------------------------

    async def async_get_folders(self) -> dict[int, str]:
        data = await self.async_send(CMD_FOLDERS, TARGET_SYSTEM, b"", use_seq=False)
        return parse_folders(data) if data else {}

    async def async_get_tracks(self, folder_index: int, folder_name: str,
                               max_pages: int = 40) -> list[str]:
        """Walk the paged 0x0024 listing until a page comes back empty."""
        name = folder_name.encode("utf-8")
        tracks: list[str] = []
        for page in range(1, max_pages + 1):
            payload = bytes((folder_index, page, len(name))) + name
            data = await self.async_send(
                CMD_TRACKS, TARGET_SYSTEM, payload, use_seq=False
            )
            if not data:
                break
            _folder, _page, names = parse_tracks(data)
            if not names:
                break
            tracks.extend(names)
        return tracks

    async def async_play_file(self, folder: str, track: str) -> None:
        payload = build_play_file(folder, track)
        await self.async_write(CMD_PLAY_FILE, TARGET_SYSTEM, payload)

    async def async_play_url(self, name: str, url: str) -> None:
        """Play any HTTP stream, using the same command the app uses for radio."""
        payload = build_play_url(name, url)
        await self.async_write(CMD_PLAY_URL, TARGET_SYSTEM, payload)
