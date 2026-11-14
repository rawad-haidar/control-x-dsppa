"""Lightweight HTTP-to-HTTPS stream proxy.

Runs inside Home Assistant and re-serves https streams over plain http, so the
amplifier's embedded players (which have no TLS) can fetch them.

A request to http://<HA>:8097/proxy?url=https://example.com/stream.mp3 is
proxied transparently.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, unquote

from aiohttp import ClientTimeout, web

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PROXY_PORT = 8097
PROXY_PATH = "/proxy"
CHUNK_SIZE = 16384
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 0  # streams are infinite


async def async_setup_proxy(hass: HomeAssistant) -> str | None:
    """Start the proxy server and return its base URL template, or None."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass, verify_ssl=False)

    async def _handle(request: web.Request) -> web.StreamResponse:
        target = request.query.get("url")
        if not target:
            return web.Response(status=400, text="Missing ?url= parameter")

        target = unquote(target)
        _LOGGER.debug("Proxying %s", target)

        try:
            upstream = await session.get(
                target,
                timeout=ClientTimeout(
                    sock_connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT
                ),
                headers={"Icy-MetaData": "0"},
            )
        except Exception as err:
            _LOGGER.warning("Proxy upstream error for %s: %s", target, err)
            return web.Response(status=502, text=str(err))

        response = web.StreamResponse(
            status=upstream.status,
            headers={
                "Content-Type": upstream.headers.get("Content-Type", "audio/mpeg"),
                "Connection": "close",
            },
        )
        await response.prepare(request)

        try:
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            upstream.close()

        return response

    app = web.Application()
    app.router.add_get(PROXY_PATH, _handle)

    runner = web.AppRunner(app)
    await runner.setup()

    try:
        site = web.TCPSite(runner, "0.0.0.0", PROXY_PORT)
        await site.start()
    except OSError as err:
        _LOGGER.error("Cannot start stream proxy on port %s: %s", PROXY_PORT, err)
        await runner.cleanup()
        return None

    _LOGGER.info("DSPPA stream proxy running on port %s", PROXY_PORT)

    async def _cleanup():
        await runner.cleanup()

    hass.data["dsppa_proxy_cleanup"] = _cleanup
    return f"http://{{}}:{PROXY_PORT}{PROXY_PATH}"


def make_proxy_url(base_template: str, ha_host: str, target_url: str) -> str:
    """Build a proxy URL that the amplifier can fetch."""
    base = base_template.format(ha_host)
    return f"{base}?url={quote(target_url)}"
