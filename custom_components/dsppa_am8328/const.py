"""Constants for the DSPPA AM8328 integration."""

from __future__ import annotations

DOMAIN = "dsppa_am8328"

CONF_DLNA_ENTITY = "dlna_entity"       # legacy: one renderer for the whole amp
CONF_DLNA_ENTITIES = "dlna_entities"   # per-zone: {"1": "media_player.kitchen", ...}


CONF_AIRPLAY_ENTITIES = "airplay_entities"  # per-zone AirPlay players to watch
CONF_AUTO_ACTIVATE = "auto_activate"
CONF_LEAVE_SOURCE = "leave_source"   # source given to a zone leaving a group

# Where a zone is parked when it leaves a group. A line input is the natural
# choice: the zone stays powered and usable, but stops hearing the shared
# renderer.
DEFAULT_LEAVE_SOURCE = 0x51  # AUX1


def dlna_key(zone: int) -> str:
    """Options key holding the DLNA entity linked to a zone."""
    return f"zone_{zone}"


def airplay_key(zone: int) -> str:
    """Options key holding the AirPlay entity watched for a zone."""
    return f"airplay_{zone}"

DEFAULT_PORT = 40188
LISTEN_PORT = 40189
DEFAULT_NAME = "AM8328"

CONTROLLER_ID = b"\xe0\x00\x20"
DEVICE_ID = b"\x52\x01\x01"

DIR_REQUEST = 0xFF
DIR_REPLY = 0x00

READ = 0x80
WRITE = 0x08

TARGET_SYSTEM = 0x00
TARGET_ALL = 0xFF

MAX_VOLUME = 100
SCAN_INTERVAL = 10
RESPONSE_TIMEOUT = 3.0

# The official app sends every tone command twice, and acknowledgements to
# writes are frequently dropped, so writes are repeated before giving up.
WRITE_ATTEMPTS = 3

# --- Commands -------------------------------------------------------------
CMD_VOLUME = 0x0001
CMD_SOURCE = 0x0002
CMD_POWER = 0x0006
CMD_PLAY_STATE = 0x0005  # 01 play, 02 pause, 04 stop - NOT a source category
CMD_TRACK_STEP = 0x000A  # 0x10 next, 0x01 previous
CMD_FOLDERS = 0x001B     # SD/USB folder list
CMD_TRACKS = 0x0024      # tracks in a folder, paged
CMD_TONE = 0x002F        # 08 <treble> <bass>
CMD_PLAY_FILE = 0x0037   # 08 <len>folder <len>track
CMD_PLAY_URL = 0x0021    # 08 <len>name <len>url - plays any HTTP stream
CMD_NOW_PLAYING = 0x0025 # unsolicited push: [family|zone][len]name[state]
CMD_TEXT = 0x000D
CMD_ZONE_NAMES = 0x000E
CMD_STATE = 0x0010  # every zone's source/volume/power/tone in one frame
CMD_INFO = 0x0011
CMD_SOURCE_LIST = 0x0022
CMD_RADIO_INDEX = 0x003A

# --- Sources --------------------------------------------------------------
# Values shared with the DSPPA serial protocol. Net radio reports as
# 0xC0 | station_index, so anything in 0xC0-0xCF is a network station.
SOURCES: dict[str, int] = {
    "Internal Music": 0x01,
    "FM": 0x11,
    "Tuner": 0x21,
    "TV": 0x31,
    "DVD": 0x41,
    "AUX1": 0x51,
    "AUX2": 0x52,
    "PC": 0x61,
    "USB": 0x81,
    "SD": 0x91,
    "Bluetooth": 0xA1,
    "DLNA": 0xB1,
    "Net Radio": 0xC1,
    "Net Music": 0xD1,
    "AirPlay": 0xE1,
}
SOURCE_NAMES: dict[int, str] = {v: k for k, v in SOURCES.items()}

RADIO_BASE = 0xC0
RADIO_MASK = 0xF0


def source_label(value: int) -> str:
    """Human label for a raw source byte, collapsing radio stations."""
    if value in SOURCE_NAMES:
        return SOURCE_NAMES[value]
    if value & RADIO_MASK == RADIO_BASE:
        return "Net Radio"
    return f"Source {value:#04x}"

# Network sources. The amplifier does not list these in its 0x0022 alias
# table, but the app offers them, so they are added to the picker manually.
# Confirmed against the amplifier's own state frames: writing 0xD1 leaves the
# zone reporting the same family as 0x81 (USB), i.e. the internal file player,
# while 0xB1 reports a distinct family - that one is the real DLNA renderer.
# The app offers six sources: the four physical inputs from the alias table,
# plus these two. 0xB1 is the DLNA renderer, which the app calls Internet
# music. 0xD1 is deliberately absent - selecting it lands on the internal file
# player, the same place 0x81 (SD/USB) goes, so it only caused confusion.
NETWORK_SOURCES: dict[int, str] = {
    0xC1: "Internet radio",
    0xB1: "Internet music",
}


# --- Playback -------------------------------------------------------------
PLAY = 0x01
PAUSE = 0x02
STOP = 0x04

STEP_NEXT = 0x10
STEP_PREVIOUS = 0x01

# Tone controls. 0x10 is flat and the amplifier uses 0x0B-0x15 - eleven steps
# - while the app presents -10..+10 dB. So one raw step is 2 dB, not 1.
TONE_FLAT = 0x10
TONE_MIN = 0x0B
TONE_MAX = 0x15
TONE_DB_PER_STEP = 2


def tone_to_db(raw: int) -> int:
    """Raw tone byte to decibels."""
    return (raw - TONE_FLAT) * TONE_DB_PER_STEP


def db_to_tone(db: float) -> int:
    """Decibels to a raw tone byte, clamped to what the amplifier accepts."""
    raw = TONE_FLAT + round(db / TONE_DB_PER_STEP)
    return max(TONE_MIN, min(TONE_MAX, raw))

TRACKS_PER_PAGE = 5
MAX_TRACK_PAGES = 40


# 0x0021 carries the stream name and URL inside one frame, and a frame's data
# section is a single length byte, so the two strings must fit in 255 bytes.
MAX_STREAM_PAYLOAD = 250


# 0x0025 pushes carry the source family in the high nibble and the ZONE in the
# low nibble - 0xC2 is zone 2 on net radio, not station 2.
def split_now_playing(byte: int) -> tuple[int, int]:
    """Return (source family nibble, zone) from a 0x0025 leading byte."""
    return byte & 0xF0, byte & 0x0F


# Selecting this source points the zone at the amplifier's DLNA renderer.
SOURCE_DLNA = 0xB1

# The amplifier's own card, used when playing a track from SD/USB.
SOURCE_SDUSB = 0x81


def is_dlna_source(source: int) -> bool:
    """DLNA source reports use family 0xB0 (e.g. Saloon B2, Kitchen B3)."""
    return source & 0xF0 == 0xB0
