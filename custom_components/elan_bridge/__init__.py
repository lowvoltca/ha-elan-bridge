"""ELAN bridge: chosen Home Assistant lights and switches appear as ELAN lighting devices, and changes go both ways.

Home Assistant connects to the HA Bridge driver on the ELAN controller (TCP 10010, protocol in
protocol.py). On every connect it sends the device list (ELAN creates each device once) and the current
states; afterwards it sends each state change, and turns ELAN's ON/OFF/DIM commands into service calls.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from . import protocol as p
from .client import BridgeClient
from .const import (
    CONF_CAMERA_PORTS,
    CONF_CAMERAS,
    CONF_DOORBELL,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_IDS,
    CONF_MEDIA,
    CONF_MEDIA_PORTS,
    CONF_NVR_CAMERAS,
    CONF_NVR_LIVE,
    CONF_SECURITY_PANEL,
    CONF_SECURITY_SENSORS,
    CONF_SECURITY_ZONES,
    CONF_NVR_PORTS,
    CONF_PORT,
    CONF_STATION_ID,
    assign_ids,
)
from .doorbell import async_setup_doorbell
from .events import async_setup_events
from .security import assign_zones, async_setup_security
from .sip_door import LEGACY_STATION_ID
from .events_logic import assign_ports as assign_camera_ports
from .media import async_setup_media
from .media_logic import assign_ports
from .nvr import assign_onvif_ports, async_setup_nvr

_LOGGER = logging.getLogger(__name__)


def device_type(state: State) -> str:
    """Lights that can dim become ELAN dimmers; everything else an on/off switch."""
    if state.domain == "light":
        modes = state.attributes.get("supported_color_modes") or []
        if any(mode != "onoff" for mode in modes):
            return p.DIMMER
    return p.SWITCH


def state_lines(dev_id: int, dev_type: str, state: State) -> list:
    on = state.state == STATE_ON
    lines = [p.state(dev_id, on)]
    if dev_type == p.DIMMER and on:
        brightness = state.attributes.get("brightness")
        if brightness is not None:
            lines.append(p.level(dev_id, round(int(brightness) * 100 / 255)))
    return lines


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if CONF_STATION_ID not in entry.data:     # set up before 0.7.0: keep the id ELAN already enrolled
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_STATION_ID: LEGACY_STATION_ID})
    entities = list(entry.options.get(CONF_ENTITIES, []))
    media = list(entry.options.get(CONF_MEDIA, []))
    ids = assign_ids(entities, entry.options.get(CONF_IDS, {}))
    ports = assign_ports(media, entry.options.get(CONF_MEDIA_PORTS, {}))
    cameras = list(entry.options.get(CONF_CAMERAS, []))
    camera_ports = assign_camera_ports(cameras, entry.options.get(CONF_CAMERA_PORTS, {}))
    nvr_cameras = list(entry.options.get(CONF_NVR_CAMERAS, []))
    nvr_ports = assign_onvif_ports(nvr_cameras, entry.options.get(CONF_NVR_PORTS, {}))
    sec_sensors = list(entry.options.get(CONF_SECURITY_SENSORS, []))
    sec_zones = assign_zones(sec_sensors, entry.options.get(CONF_SECURITY_ZONES, {}))
    if (ids != entry.options.get(CONF_IDS) or ports != entry.options.get(CONF_MEDIA_PORTS)
            or camera_ports != entry.options.get(CONF_CAMERA_PORTS) or nvr_ports != entry.options.get(CONF_NVR_PORTS)
            or sec_zones != entry.options.get(CONF_SECURITY_ZONES, {})):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_IDS: ids, CONF_MEDIA_PORTS: ports, CONF_CAMERA_PORTS: camera_ports,
                            CONF_NVR_PORTS: nvr_ports, CONF_SECURITY_ZONES: sec_zones})
    by_id = {ids[e]: e for e in entities}
    added: dict = {}            # entity id -> type, sent this connection

    async def add_and_report(entity_id: str, state: State) -> None:
        dev_id = ids[entity_id]
        if entity_id not in added:
            dev_type = device_type(state)
            name = state.attributes.get("friendly_name") or entity_id
            if not await client.send(p.add(dev_id, dev_type, name)):
                return
            added[entity_id] = dev_type
        for line in state_lines(dev_id, added[entity_id], state):
            await client.send(line)

    async def on_connect() -> None:
        added.clear()
        await client.send(p.hello())
        for entity_id in entities:
            state = hass.states.get(entity_id)
            # An entity that isn't up yet is added when it is, so it gets the right type (dimmer or switch).
            if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                await add_and_report(entity_id, state)

    @callback
    def on_state(event: Event) -> None:
        state = event.data.get("new_state")
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or not client.connected:
            return
        hass.async_create_task(add_and_report(state.entity_id, state))

    @callback
    def on_command(cmd: p.Command) -> None:
        entity_id = by_id.get(cmd.dev_id)
        if entity_id is None:
            _LOGGER.debug("ELAN sent %s for unknown device %s", cmd.verb, cmd.dev_id)
            return
        domain = entity_id.split(".")[0]
        service_domain = "light" if domain == "light" else "homeassistant"
        data = {ATTR_ENTITY_ID: entity_id}
        if cmd.verb == "ON":
            service = "turn_on"
        elif cmd.verb == "OFF":
            service = "turn_off"
        elif cmd.verb == "DIM":
            pct = int(cmd.value)
            service = "turn_on" if pct > 0 else "turn_off"
            if pct > 0 and domain == "light":
                data["brightness_pct"] = pct
        else:
            _LOGGER.debug("ELAN %s %s %s not handled yet", cmd.verb, cmd.dev_id, cmd.value)
            return
        _LOGGER.debug("ELAN -> %s.%s %s", service_domain, service, data)
        hass.async_create_task(hass.services.async_call(service_domain, service, data, blocking=False))

    @callback
    def on_connection(up: bool) -> None:
        _LOGGER.info("ELAN bridge %s:%s %s", entry.data[CONF_HOST], entry.data[CONF_PORT],
                     "connected" if up else "disconnected")

    client = BridgeClient(entry.data[CONF_HOST], entry.data[CONF_PORT], on_command, on_connect, on_connection)
    clients = [client]
    entry.runtime_data = clients
    if entities:
        entry.async_on_unload(async_track_state_change_event(hass, entities, on_state))
        await client.start()      # no lighting entities: don't hold a connection to the lighting driver
    links = await async_setup_media(hass, entry.data[CONF_HOST], ports, media, entry.async_on_unload)
    clients.extend(link.client for link in links)
    cams = await async_setup_events(hass, entry.data[CONF_HOST], camera_ports, cameras, entry.async_on_unload)
    clients.extend(link.client for link in cams)
    security = await async_setup_security(hass, entry.data[CONF_HOST], sec_sensors, sec_zones,
                                          entry.options.get(CONF_SECURITY_PANEL), entry.async_on_unload)
    if security:
        clients.append(security.client)
    try:     # NVR playback is optional: a problem there must not take the lights/media bridge down
        await async_setup_nvr(hass, entry.entry_id, nvr_cameras, nvr_ports, entry.async_on_unload,
                              entry.options.get(CONF_NVR_LIVE, {}))
    except Exception:
        _LOGGER.exception("ELAN NVR emulator didn't start")
    try:     # same: the doorbell call is optional
        await async_setup_doorbell(hass, entry.data[CONF_HOST], entry.options.get(CONF_DOORBELL),
                                   entry.data[CONF_STATION_ID], entry.async_on_unload)
    except Exception:
        _LOGGER.exception("ELAN doorbell didn't start")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for client in entry.runtime_data:
        await client.close()
    return True
