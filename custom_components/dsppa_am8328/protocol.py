"""Frame encoding and parsing for the DSPPA AM8328 LAN protocol.

Reverse-engineered from the official control app. Frame layout:

    [cmd:2] [seq:1] [dir:1] [id:3] [target:1] [len:1] [data:len] [xor:1]

    dir     0xff request, 0x00 reply
    id      e0 00 20 controller, 52 01 01 device
    target  zone 1-12, 0x00 system-wide, 0xff all zones
    data    0x80 ...    read a property
            0x08 value  write a value
    xor     XOR of the data bytes only (not the header)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce

from .const import CONTROLLER_ID, DIR_REQUEST

HEADER_LEN = 9  # cmd(2) seq(1) dir(1) id(3) target(1) len(1)
MIN_FRAME_LEN = HEADER_LEN + 1


class ProtocolError(Exception):
    """A frame could not be built or parsed."""


def xor(data: bytes) -> int:
    return reduce(lambda a, b: a ^ b, data, 0)


def build_frame(cmd: int, target: int, data: bytes = b"", seq: int = 0) -> bytes:
    """Build a request frame."""
    if len(data) > 0xFF:
        raise ProtocolError(f"data too long: {len(data)} bytes")
    return (
        cmd.to_bytes(2, "big")
        + bytes((seq & 0xFF, DIR_REQUEST))
        + CONTROLLER_ID
        + bytes((target, len(data)))
        + data
        + bytes((xor(data),))
    )


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded frame."""

    cmd: int
    seq: int
    direction: int
    identity: bytes
    target: int
    data: bytes

    def __str__(self) -> str:
        return (
            f"cmd={self.cmd:#06x} seq={self.seq} target={self.target:#04x} "
            f"data={self.data.hex(' ') or '-'}"
        )


def parse_frame(payload: bytes) -> Frame | None:
    """Parse a datagram, returning None if it is not a valid frame."""
    if len(payload) < MIN_FRAME_LEN:
        return None
    length = payload[8]
    if len(payload) != MIN_FRAME_LEN + length:
        return None
    data = payload[9 : 9 + length]
    if xor(data) != payload[9 + length]:
        return None
    return Frame(
        cmd=int.from_bytes(payload[0:2], "big"),
        seq=payload[2],
        direction=payload[3],
        identity=payload[4:7],
        target=payload[7],
        data=data,
    )


# --- payload parsers ------------------------------------------------------


@dataclass(slots=True)
class ZoneState:
    """One zone's entry in the 0x0010 state frame."""

    zone: int
    source: int
    volume: int
    power: bool
    treble: int
    bass: int


ZONE_RECORD_LEN = 6


def parse_state(data: bytes) -> dict[int, ZoneState]:
    """Parse the 0x0010 frame: count byte then 6 bytes per zone."""
    if not data:
        return {}
    count = data[0]
    zones: dict[int, ZoneState] = {}
    for index in range(count):
        start = 1 + index * ZONE_RECORD_LEN
        record = data[start : start + ZONE_RECORD_LEN]
        if len(record) < ZONE_RECORD_LEN:
            break
        zones[record[0]] = ZoneState(
            zone=record[0],
            source=record[1],
            volume=record[2],
            power=bool(record[3]),
            treble=record[4],
            bass=record[5],
        )
    return zones


def parse_named_list(data: bytes) -> dict[int, str]:
    """Parse a count-prefixed list of (id, length, name) entries.

    Used by 0x000E (zone names) and 0x002B.
    """
    if not data:
        return {}
    names: dict[int, str] = {}
    offset = 1  # skip the count
    while offset + 1 < len(data):
        key = data[offset]
        length = data[offset + 1]
        name = data[offset + 2 : offset + 2 + length]
        if len(name) < length:
            break
        names[key] = name.decode("utf-8", "replace").strip()
        offset += 2 + length
    return names


def parse_source_list(data: bytes) -> dict[int, str]:
    """Parse 0x0022: (source value, length, alias) entries, no count prefix."""
    sources: dict[int, str] = {}
    offset = 0
    while offset + 1 < len(data):
        value = data[offset]
        length = data[offset + 1]
        name = data[offset + 2 : offset + 2 + length]
        if len(name) < length:
            break
        sources[value] = name.decode("utf-8", "replace").strip()
        offset += 2 + length
    return sources


def parse_pairs(data: bytes) -> dict[int, int]:
    """Parse simple (key, value) byte pairs, as used by 0x003A and 0x0035."""
    return {data[i]: data[i + 1] for i in range(0, len(data) - 1, 2)}


def parse_info(data: bytes) -> list[str]:
    """Parse 0x0011: consecutive length-prefixed strings."""
    fields: list[str] = []
    offset = 0
    while offset < len(data):
        length = data[offset]
        fields.append(data[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return fields


def parse_folders(data: bytes) -> dict[int, str]:
    """Parse 0x001B: count, then (index, length, name, 0x00) per folder."""
    if not data:
        return {}
    folders: dict[int, str] = {}
    count = data[0]
    offset = 1
    for _ in range(count):
        if offset + 1 >= len(data):
            break
        index = data[offset]
        length = data[offset + 1]
        name = data[offset + 2 : offset + 2 + length]
        if len(name) < length:
            break
        folders[index] = name.decode("utf-8", "replace").strip()
        offset += 2 + length + 1  # trailing separator byte
    return folders


def parse_tracks(data: bytes) -> tuple[int, int, list[str]]:
    """Parse 0x0024: folder index, page number, then length-prefixed names."""
    if len(data) < 2:
        return 0, 0, []
    folder, page = data[0], data[1]
    tracks: list[str] = []
    offset = 2
    while offset < len(data):
        length = data[offset]
        name = data[offset + 1 : offset + 1 + length]
        if not length or len(name) < length:
            break
        tracks.append(name.decode("utf-8", "replace"))
        offset += 1 + length
    return folder, page, tracks


def build_play_file(folder: str, track: str) -> bytes:
    """Build the 0x0037 payload: write marker, then two length-prefixed names."""
    f = folder.encode("utf-8")
    t = track.encode("utf-8")
    return bytes((0x08, len(f))) + f + bytes((len(t),)) + t


def build_play_url(name: str, url: str) -> bytes:
    """Build the 0x0021 payload: write marker, then name and URL, each length-prefixed."""
    n = name.encode("utf-8")[:60]
    u = url.encode("utf-8")
    if len(n) + len(u) + 3 > 255:
        raise ProtocolError(
            f"stream name and URL are too long for one frame "
            f"({len(n) + len(u) + 3} bytes)"
        )
    return bytes((0x08, len(n))) + n + bytes((len(u),)) + u


def parse_now_playing(data: bytes) -> tuple[int, str, int] | None:
    """Parse a 0x0025 push: [family|zone] [len] name [play state] 00 00 00."""
    if len(data) < 2:
        return None
    zone = data[0] & 0x0F
    length = data[1]
    name = data[2 : 2 + length]
    if len(name) < length:
        return None
    state = data[2 + length] if len(data) > 2 + length else 0
    return zone, name.decode("utf-8", "replace"), state
