"""Device registry helpers.

Each zone is registered as its own device so it can be assigned to a Home
Assistant area (Gym, Saloon, Pool...). All zone devices are linked back to the
amplifier through `via_device_id`, so the hardware relationship stays visible in
the UI while the rooms remain independent.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def amplifier_identifier(entry_id: str) -> tuple[str, str]:
    return (DOMAIN, entry_id)


def zone_identifier(entry_id: str, zone: int) -> tuple[str, str]:
    return (DOMAIN, f"{entry_id}_zone_{zone}")


def amplifier_device_info(entry_id: str, title: str, info: list[str]) -> DeviceInfo:
    """The amplifier itself — the parent device."""
    return DeviceInfo(
        identifiers={amplifier_identifier(entry_id)},
        manufacturer="DSPPA",
        model=info[0] if info else "AM8328",
        sw_version=info[1] if len(info) > 1 else None,
        name=title,
    )


def zone_device_info(
    entry_id: str,
    title: str,
    zone: int,
    zone_name: str | None,
    amp_device_id: str | None = None,
) -> DeviceInfo:
    """One zone, as a child device that can live in its own area."""
    info = DeviceInfo(
        identifiers={zone_identifier(entry_id, zone)},
        manufacturer="DSPPA",
        model="AM8328 zone",
        name=zone_name or f"Zone {zone}",
        suggested_area=zone_name or None,
    )
    if amp_device_id:
        info["via_device_id"] = amp_device_id
    return info
