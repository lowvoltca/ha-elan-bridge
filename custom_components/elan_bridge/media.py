"""Media players shown in ELAN: one connection per player, to that player's HA Media Player source driver.

Home Assistant pushes what the ELAN media page shows (title, source, artwork, position, play/pause highlight) on
every state change; page buttons come back as service calls on the player, or on the remote entity of the same
device for the arrow, OK, Home and Menu keys. Every button is also fired as an `elan_bridge_media_button` event
so automations can use the spare keys (Custom1-5).
"""

from __future__ import annotations

import logging

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from . import media_logic as m
from . import protocol as p
from .client import BridgeClient

_LOGGER = logging.getLogger(__name__)
EVENT_BUTTON = "elan_bridge_media_button"


def _cover_base(hass: HomeAssistant) -> str:
    """Home Assistant's own LAN address, so panels can load artwork from it."""
    try:
        from homeassistant.helpers.network import get_url
        return get_url(hass, allow_internal=True, allow_external=False, prefer_external=False)
    except Exception:          # no internal URL configured: show no artwork rather than fail
        return ""


def _remote_for(hass: HomeAssistant, entity_id: str):
    """(remote entity id, its platform) on the same device as the media player, if any."""
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None, None
    for other in er.async_entries_for_device(reg, entry.device_id):
        if other.domain == "remote" and not other.disabled_by:
            return other.entity_id, other.platform
    return None, None


class MediaLink:
    def __init__(self, hass: HomeAssistant, host: str, port: int, entity_id: str) -> None:
        self.hass, self.entity_id = hass, entity_id
        self.remote, self.remote_platform = _remote_for(hass, entity_id)
        self.cover_base = _cover_base(hass)
        self.sent: dict = {}
        self.client = BridgeClient(host, port, self.on_command, self.on_connect)

    async def start(self) -> None:
        await self.client.start()

    async def push(self) -> None:
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return
        values = m.page_values(state.state, dict(state.attributes), self.cover_base)
        for line in m.page_lines(values, m.button_states(state.state), self.sent):
            if not await self.client.send(line):
                return

    async def on_connect(self) -> None:
        self.sent.clear()
        await self.client.send(p.hello())
        await self.push()

    @callback
    def on_state(self, event: Event) -> None:
        if self.client.connected:
            self.hass.async_create_task(self.push())

    @callback
    def on_command(self, cmd: p.Command) -> None:
        if cmd.verb not in ("FUNC", "REPEAT"):
            return
        button = cmd.value
        if cmd.verb == "REPEAT" and button not in m.REPEATING:
            return
        if cmd.verb == "FUNC":
            self.hass.bus.async_fire(EVENT_BUTTON, {ATTR_ENTITY_ID: self.entity_id, "button": button})
        state = self.hass.states.get(self.entity_id)
        attrs = dict(state.attributes) if state else {}
        act = m.action(button, attrs, self.remote, self.remote_platform)
        if act is None:
            _LOGGER.debug("%s: ELAN button %r has no action (event only)", self.entity_id, button)
            return
        domain, service, data, target = act
        data = {**data, ATTR_ENTITY_ID: self.remote if target == "remote" else self.entity_id}
        _LOGGER.debug("ELAN %s -> %s.%s %s", button, domain, service, data)
        self.hass.async_create_task(self.hass.services.async_call(domain, service, data, blocking=False))


async def async_setup_media(hass: HomeAssistant, host: str, ports: dict, entities: list, on_unload) -> list:
    links = []
    for entity_id in entities:
        link = MediaLink(hass, host, ports[entity_id], entity_id)
        on_unload(async_track_state_change_event(hass, [entity_id], link.on_state))
        await link.start()
        links.append(link)
    return links
