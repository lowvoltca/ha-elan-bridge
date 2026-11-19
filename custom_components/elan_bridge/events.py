"""Camera analytics shown in ELAN: one connection per camera device, to its HA Camera Events driver.

Each chosen device's binary sensors and event entities (UniFi Protect names: Person detected, Doorbell, Motion,
...) are mapped to the ELAN events the driver declares; a change is sent as EVENT <name> On|Off, which the driver
raises for Event Mapper. Nothing is sent on (re)connect, so rules don't fire just because Home Assistant restarted.
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import events_logic as ev
from . import protocol as p
from .client import BridgeClient

_LOGGER = logging.getLogger(__name__)


def camera_entities(hass: HomeAssistant, device_id: str) -> dict:
    """entity id -> classification, for the device's mappable sensors."""
    out = {}
    for entry in er.async_entries_for_device(er.async_get(hass), device_id):
        if entry.disabled_by:
            continue
        kind = ev.classify(entry.domain, [entry.original_name, entry.translation_key], entry.entity_id)
        if kind:
            out[entry.entity_id] = kind
    return out


class CameraLink:
    def __init__(self, hass: HomeAssistant, host: str, port: int, device_id: str) -> None:
        self.hass = hass
        self.entities = camera_entities(hass, device_id)
        self.states = ev.EventStates()
        self.client = BridgeClient(host, port, lambda cmd: None, self.on_connect)
        for entity_id, kind in self.entities.items():
            state = hass.states.get(entity_id)
            if kind[0] == "binary" and state is not None:
                self.states.set(kind[1], entity_id, state.state == STATE_ON)

    async def on_connect(self) -> None:
        await self.client.send(p.hello())

    def _send(self, line: str) -> None:
        if self.client.connected:
            self.hass.async_create_task(self.client.send(line))

    def _now(self) -> str:
        return dt_util.now().strftime("%Y-%m-%d %H:%M:%S")

    @callback
    def on_state(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new, old = event.data.get("new_state"), event.data.get("old_state")
        kind = self.entities.get(entity_id)
        if kind is None or new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if kind[0] == "binary":
            changed = self.states.set(kind[1], entity_id, new.state == STATE_ON)
            if changed is not None:
                self._send(p.camera_event(kind[1], changed))
                if changed and kind[1] == "Motion Detected":
                    self._send(p.named_var(ev.LAST_MOTION, self._now()))
            return
        # event entity: a new timestamp means it fired (a restore from unavailable doesn't count)
        if old is None or old.state in (STATE_UNAVAILABLE,) or old.state == new.state:
            return
        _, elan_event, variable = kind
        if variable:
            self._send(p.named_var(variable, self._now()))
            event_type = new.attributes.get("event_type")
            if event_type:
                self._send(p.named_var(variable + " Type", str(event_type)))
        if elan_event:
            # Shares the On/Off tracking with binary sensors of the same event (a doorbell is both), so ELAN
            # gets one On and one Off.
            if self.states.set(elan_event, entity_id, True):
                self._send(p.camera_event(elan_event, True))

            @callback
            def off(_now) -> None:
                if self.states.set(elan_event, entity_id, False) is False:
                    self._send(p.camera_event(elan_event, False))

            async_call_later(self.hass, ev.PULSE_SECONDS, off)


async def async_setup_events(hass: HomeAssistant, host: str, ports: dict, devices: list, on_unload) -> list:
    links = []
    for device_id in devices:
        link = CameraLink(hass, host, ports[device_id], device_id)
        if not link.entities:
            _LOGGER.warning("Camera device %s has no detection sensors ELAN can use", device_id)
        else:
            on_unload(async_track_state_change_event(hass, list(link.entities), link.on_state))
        await link.client.start()
        links.append(link)
    return links
