# Control-X for DSPPA AM8328

Control your DSPPA AM8328 multi-zone amplifier from Home Assistant over your local network. Packaged from the supplied working integration, with a configuration flow and optional DLNA/AirPlay player links for each zone.

This is a **Home Assistant custom integration**, installed through HACS or manually. It is not a standalone mobile app or a Home Assistant Supervisor app.

[![Install in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rawad-haidar&repository=control-x-dsppa&category=integration)

## Installation

1. Click **Install in HACS**, add the repository with category **Integration**, then download it. Alternatively add `https://github.com/rawad-haidar/control-x-dsppa` in HACS custom repositories.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Control-X for DSPPA AM8328**.
4. Enter the amplifier IP address and control port (default **40188**), then choose a name.
5. Open the integration's options to link each zone to an existing DLNA renderer and, optionally, an AirPlay player.

For manual installation, download the release's Source code ZIP, extract it and copy `custom_components/dsppa_am8328` into your Home Assistant configuration directory:

```text
config/custom_components/dsppa_am8328/manifest.json
config/custom_components/dsppa_am8328/__init__.py
```

Restart Home Assistant and add the integration as above. Do not copy the outer repository folder into `custom_components`.

## Controls and options

- Per-zone media players: power, volume, mute and source selection.
- Per-zone bass and treble number entities.
- Playback and media browsing where supported by the selected source and amplifier.
- Optional DLNA renderer and AirPlay player links for individual zones.
- Optional automatic zone wake-up when linked players start playback.
- Grouping for zones sharing a renderer; a configurable source for a zone leaving a group.

Features depend on the amplifier, firmware, source and linked player. Separate DLNA/AirPlay integrations are needed to provide entities for the optional links; this package does not install those integrations.

## Network and troubleshooting

Home Assistant must be able to reach the amplifier on the local network. The integration sends UDP control traffic to port **40188** by default and listens on UDP **40189**. If setup reports a bind error, check for another process using port 40189. If the amplifier does not reply, check its power, IP address and LAN/firewall connectivity.

Direct amplifier URL playback requires an HTTP stream. For HTTPS streams, link a suitable DLNA player in the options. The supplied source includes an unused experimental proxy module; normal setup does not start it. It is not a supported public network service.

## Updating an existing installation

The internal domain and folder remain **`dsppa_am8328`** so existing configuration entries can be retained. Back up the existing integration and Home Assistant configuration, replace the integration folder, then restart. Do not install duplicate copies or delete your existing configuration entry just to update. The Control-X name changes the integration's display label, not the device protocol.

## Version 1.0.7

Initial Control-X installation edition, based on supplied source version 1.0.6. Changes cover branding, repository metadata, installation documentation, a brand icon and package validation. Device-control logic is unchanged. Backup files and Python caches are excluded; no household configuration or credentials are included.

Python syntax, JSON and distribution layout are checked locally and in GitHub Actions. A fresh Home Assistant installation and physical amplifier behavior have not been retested for this release. Compatibility with other DSPPA models is not claimed.

## Source and rights

See [NOTICE.md](NOTICE.md). The supplied folder contained no license or upstream repository attribution; no open-source license has been invented or applied to that source. This repository is an independent community package and is not endorsed by DSPPA.
