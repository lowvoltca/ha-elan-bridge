"""Setup: the ELAN controller's address, then which entities to show in ELAN. Options: change the entities."""

from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import protocol as p
from .const import (
    BRIDGED_DOMAINS,
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
    DOMAIN,
    assign_ids,
    new_station_id,
)
from .events_logic import assign_ports as assign_camera_ports
from .media_logic import assign_ports
from .nvr import assign_onvif_ports, async_live_choices
from .security import assign_zones


async def _answers(host: str, port: int) -> bool:
    """HELLO, the way the integration starts each connection, to check the HA Bridge driver is listening."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write((p.hello() + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(256), 5)
        return any(line.startswith("HELLO") for line in p.LineBuffer().feed(data))
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()


SECURITY_CLASSES = ["door", "window", "garage_door", "opening", "motion", "occupancy", "presence", "vibration",
                    "moisture", "smoke", "gas", "carbon_monoxide", "tamper", "safety"]


def _entities_schema(default: list, media_default: list, camera_default: list = (), nvr_default: list = (),
                     sec_default: list = (), panel_default: str | None = None,
                     doorbell_default: str | None = None) -> vol.Schema:
    return vol.Schema({
        vol.Optional(CONF_ENTITIES, default=default): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=BRIDGED_DOMAINS, multiple=True)),
        vol.Optional(CONF_MEDIA, default=media_default): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player", multiple=True)),
        vol.Optional(CONF_CAMERAS, default=list(camera_default)): selector.DeviceSelector(
            selector.DeviceSelectorConfig(entity=selector.EntityFilterSelectorConfig(domain="camera"), multiple=True)),
        vol.Optional(CONF_NVR_CAMERAS, default=list(nvr_default)): selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="unifiprotect",
                                          entity=selector.EntityFilterSelectorConfig(domain="camera"), multiple=True)),
        vol.Optional(CONF_SECURITY_SENSORS, default=list(sec_default)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="binary_sensor", device_class=SECURITY_CLASSES, multiple=True)),
        vol.Optional(CONF_SECURITY_PANEL, description={"suggested_value": panel_default}): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="alarm_control_panel")),
        vol.Optional(CONF_DOORBELL, description={"suggested_value": doorbell_default}): selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="unifiprotect",
                                          entity=selector.EntityFilterSelectorConfig(domain="camera"))),
    })


def _options(user_input: dict, old: dict) -> dict:
    entities = user_input.get(CONF_ENTITIES, [])
    media = user_input.get(CONF_MEDIA, [])
    cameras = user_input.get(CONF_CAMERAS, [])
    nvr = user_input.get(CONF_NVR_CAMERAS, [])
    # Numbers and ports of removed entities are kept, so re-adding one later reuses its ELAN device / driver.
    return {CONF_ENTITIES: entities, CONF_IDS: assign_ids(entities, old.get(CONF_IDS, {})),
            CONF_MEDIA: media, CONF_MEDIA_PORTS: assign_ports(media, old.get(CONF_MEDIA_PORTS, {})),
            CONF_CAMERAS: cameras, CONF_CAMERA_PORTS: assign_camera_ports(cameras, old.get(CONF_CAMERA_PORTS, {})),
            CONF_NVR_CAMERAS: nvr, CONF_NVR_PORTS: assign_onvif_ports(nvr, old.get(CONF_NVR_PORTS, {})),
            CONF_NVR_LIVE: {d: v for d, v in old.get(CONF_NVR_LIVE, {}).items() if d in nvr},
            CONF_SECURITY_SENSORS: user_input.get(CONF_SECURITY_SENSORS, []),
            CONF_SECURITY_ZONES: assign_zones(user_input.get(CONF_SECURITY_SENSORS, []), old.get(CONF_SECURITY_ZONES, {})),
            CONF_SECURITY_PANEL: user_input.get(CONF_SECURITY_PANEL) or None,
            CONF_DOORBELL: user_input.get(CONF_DOORBELL) or None}


def _ports_text(options: dict, device_name=lambda device_id: device_id) -> str:
    ports = options.get(CONF_MEDIA_PORTS, {})
    lines = [f"- {entity}: port **{ports[entity]}**" for entity in options.get(CONF_MEDIA, []) if entity in ports]
    cports = options.get(CONF_CAMERA_PORTS, {})
    lines += [f"- camera {device_name(d)}: port **{cports[d]}**" for d in options.get(CONF_CAMERAS, []) if d in cports]
    nports = options.get(CONF_NVR_PORTS, {})
    lines += [f"- NVR camera {device_name(d)}: ONVIF port **{nports[d]}**"
              for d in options.get(CONF_NVR_CAMERAS, []) if d in nports]
    zones = options.get(CONF_SECURITY_ZONES, {})
    if options.get(CONF_SECURITY_SENSORS) or options.get(CONF_SECURITY_PANEL):
        lines.append("- security: HA Security System driver, port **10040**; zones "
                     + ", ".join(f"{zones[e]} = {e}" for e in options.get(CONF_SECURITY_SENSORS, []) if e in zones))
    if options.get(CONF_DOORBELL):
        lines.append(f"- doorbell {device_name(options[CONF_DOORBELL])}: rings ELAN as a SIP Station")
    return "\n".join(lines) if lines else "No media players or cameras yet."


class BridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "BridgeOptionsFlow":
        return BridgeOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            if await _answers(user_input[CONF_HOST], user_input[CONF_PORT]):
                self._data = {**user_input, CONF_STATION_ID: new_station_id()}
                return await self.async_step_entities()
            errors["base"] = "cannot_connect"
        schema = vol.Schema({
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=p.PORT): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_entities(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"ELAN bridge {self._data[CONF_HOST]}", data=self._data, options=_options(user_input, {}))
        return self.async_show_form(step_id="entities", data_schema=_entities_schema([], []))


class BridgeOptionsFlow(OptionsFlowWithReload):
    def __init__(self) -> None:
        self._pending: dict = {}
        self._live_keys: dict = {}     # form field (camera name) -> device id

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            self._pending = _options(user_input, options)
            if self._pending[CONF_NVR_CAMERAS]:
                return await self.async_step_live()
            return self.async_create_entry(data=self._pending)
        return self.async_show_form(
            step_id="init",
            data_schema=_entities_schema(options.get(CONF_ENTITIES, []), options.get(CONF_MEDIA, []),
                                         options.get(CONF_CAMERAS, []), options.get(CONF_NVR_CAMERAS, []),
                                         options.get(CONF_SECURITY_SENSORS, []), options.get(CONF_SECURITY_PANEL),
                                         options.get(CONF_DOORBELL)),
            description_placeholders={"ports": _ports_text(options, self._device_name)})

    async def async_step_live(self, user_input=None) -> ConfigFlowResult:
        """Which stream each NVR camera's live view in ELAN uses."""
        live = dict(self._pending.get(CONF_NVR_LIVE, {}))
        if user_input is not None:
            for field, device_id in self._live_keys.items():
                live[device_id] = user_input.get(field, "auto")
            return self.async_create_entry(data={**self._pending, CONF_NVR_LIVE: live})
        fields, self._live_keys = {}, {}
        for device_id in self._pending[CONF_NVR_CAMERAS]:
            field = self._device_name(device_id)
            while field in self._live_keys:
                field += " "
            self._live_keys[field] = device_id
            choices = await async_live_choices(self.hass, device_id)
            current = live.get(device_id, "auto")
            if current not in [c["value"] for c in choices]:
                current = "auto"
            fields[vol.Optional(field, default=current)] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=c["value"], label=c["label"]) for c in choices],
                mode=selector.SelectSelectorMode.DROPDOWN))
        return self.async_show_form(step_id="live", data_schema=vol.Schema(fields))

    def _device_name(self, device_id: str) -> str:
        from homeassistant.helpers import device_registry as dr
        device = dr.async_get(self.hass).async_get(device_id)
        return (device.name_by_user or device.name) if device else device_id
