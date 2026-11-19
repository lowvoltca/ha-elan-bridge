"""Home Assistant door/window/motion sensors as zones of an ELAN security partition, via the HA Security System
driver (network security type, port 10040). An optional HA alarm panel is that partition: ELAN's Arm Away / Arm
Stay / Disarm call it, and its state shows in ELAN. Without a panel the partition only shows zones.

HA -> ELAN: PARTITION 1 <name>, ZONE <id> 1 <name>, ZSTATE <id> OPEN|CLOSED|TROUBLE|OK,
            PSTATE 1 DISARMED|AWAY|STAY|NIGHT|ARMING|PENDING|TRIGGERED|NOPANEL
ELAN -> HA: ARM 1 <mode> <pin|->, DISARM 1 <pin|->, STOPALARM 1 <pin|->, BYPASS <zone>, UNBYPASS <zone>
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from . import protocol as p
from .client import BridgeClient
from .const import SECURITY_PORT

_LOGGER = logging.getLogger(__name__)

PARTITION = 1
PANEL_STATE = {"disarmed": "DISARMED", "armed_away": "AWAY", "armed_home": "STAY", "armed_night": "NIGHT",
               "armed_vacation": "AWAY", "armed_custom_bypass": "STAY", "arming": "ARMING", "pending": "PENDING",
               "triggered": "TRIGGERED"}
ARM_SERVICE = {"1": "alarm_arm_away", "2": "alarm_arm_home", "3": "alarm_arm_night"}


def assign_zones(sensors: list, zones: dict) -> dict:
    """Keep every zone number already given out (ELAN's zones point at them) and number new sensors from 1."""
    zones = dict(zones)
    nxt = max([0, *zones.values()]) + 1
    for entity_id in sensors:
        if entity_id not in zones:
            zones[entity_id] = nxt
            nxt += 1
    return zones


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    name = (state and state.attributes.get("friendly_name")) or entity_id
    return " ".join(str(name).split())[:60]


def zone_line(state) -> str | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return "TROUBLE"
    return "OPEN" if state.state == STATE_ON else "CLOSED"


def panel_line(state) -> str:
    if state is None:
        return "DISARMED"
    return PANEL_STATE.get(state.state, "DISARMED")


class SecurityLink:
    def __init__(self, hass: HomeAssistant, host: str, sensors: list, zones: dict, panel: str | None) -> None:
        self.hass, self.panel = hass, panel or None
        self.zones = {e: zones[e] for e in sensors if e in zones}
        self.trouble: set = set()
        self.client = BridgeClient(host, SECURITY_PORT, self.on_command, self.on_connect)

    def _send(self, line: str) -> None:
        if self.client.connected:
            self.hass.async_create_task(self.client.send(line))

    def _pstate(self) -> str:
        return panel_line(self.hass.states.get(self.panel)) if self.panel else "NOPANEL"

    async def on_connect(self) -> None:
        send = self.client.send
        await send(p.hello())
        name = _name(self.hass, self.panel) if self.panel else "Home Assistant"
        await send(f"PARTITION {PARTITION} {name}")
        self.trouble.clear()
        for entity_id, zone in self.zones.items():
            await send(f"ZONE {zone} {PARTITION} {_name(self.hass, entity_id)}")
            st = zone_line(self.hass.states.get(entity_id))
            await send(f"ZSTATE {zone} {'OK' if st != 'TROUBLE' else 'TROUBLE'}")
            if st == "TROUBLE":
                self.trouble.add(entity_id)
            else:
                await send(f"ZSTATE {zone} {st}")
        await send(f"PSTATE {PARTITION} {self._pstate()}")

    @callback
    def on_state(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new = event.data.get("new_state")
        if entity_id == self.panel:
            self._send(f"PSTATE {PARTITION} {self._pstate()}")
            return
        zone = self.zones.get(entity_id)
        if zone is None:
            return
        st = zone_line(new)
        if st == "TROUBLE":
            if entity_id not in self.trouble:
                self.trouble.add(entity_id)
                self._send(f"ZSTATE {zone} TROUBLE")
            return
        if entity_id in self.trouble:
            self.trouble.discard(entity_id)
            self._send(f"ZSTATE {zone} OK")
        self._send(f"ZSTATE {zone} {st}")

    @callback
    def on_command(self, cmd: p.Command) -> None:
        if cmd.verb in ("BYPASS", "UNBYPASS"):
            _LOGGER.info("ELAN %s zone %s (shown in ELAN only)", cmd.verb.lower(), cmd.dev_id)
            return
        if not self.panel:
            _LOGGER.warning("ELAN asked to %s, but no alarm panel is chosen in ELAN Bridge's options", cmd.verb.lower())
            self._send(f"PSTATE {PARTITION} NOPANEL")
            return
        words = (cmd.value or "").split()
        if cmd.verb == "ARM":
            service, pin = ARM_SERVICE.get(words[0] if words else "1", "alarm_arm_away"), words[1:2]
        else:                                         # DISARM, STOPALARM
            service, pin = "alarm_disarm", words[:1]
        data = {"entity_id": self.panel}
        if pin and pin[0] != "-":
            data["code"] = pin[0]
        _LOGGER.debug("ELAN -> %s %s", service, self.panel)
        self.hass.async_create_task(
            self.hass.services.async_call("alarm_control_panel", service, data, blocking=False))



async def async_setup_security(hass: HomeAssistant, host: str, sensors: list, zones: dict, panel: str | None,
                               on_unload) -> SecurityLink | None:
    if not sensors and not panel:
        return None
    link = SecurityLink(hass, host, sensors, zones, panel)
    watched = list(link.zones) + ([panel] if panel else [])
    on_unload(async_track_state_change_event(hass, watched, link.on_state))
    await link.client.start()
    return link
