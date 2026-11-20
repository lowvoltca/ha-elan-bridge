"""A Protect doorbell rings ELAN's touchpanels: on a ring Home Assistant calls ELAN as a SIP door station
(sip_door.py) and sends the doorbell's live video and audio once a panel answers."""

from __future__ import annotations

import logging

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .nvr import (PREFERRED_LIVE_WIDTH, _ffmpeg_binary, _protect_api, _protect_camera, _split_login,
                  async_protect_streams)
from .sip_door import DoorStation

_LOGGER = logging.getLogger(__name__)
SERVICE_RING = "ring_doorbell"
FRESH_PRESS = 30      # seconds: an event entity's ring time older than this is a restored value, not a press


def ring_entities(hass: HomeAssistant, device_id: str) -> list:
    """The doorbell button's entities on the device: Protect's 'Doorbell' binary sensor and/or ring event."""
    out = []
    for entry in er.async_entries_for_device(er.async_get(hass), device_id):
        # Not "doorbell" anywhere in the id: the device itself is often named "...Doorbell...".
        names = {str(x).lower() for x in (entry.original_name, entry.translation_key) if x}
        if entry.disabled_by or not ("doorbell" in names or entry.entity_id.endswith("_doorbell")):
            continue
        if entry.domain in ("binary_sensor", "event"):
            out.append(entry.entity_id)
    return out


async def async_setup_doorbell(hass: HomeAssistant, controller: str, device_id: str | None, station_id: str,
                               on_unload):
    """Register with ELAN's SIP Server and ring it whenever the doorbell is pressed. Returns the station or None."""
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        _LOGGER.warning("ELAN doorbell: the chosen doorbell device no longer exists")
        return None
    name = device.name_by_user or device.name or "Doorbell"
    triggers = ring_entities(hass, device_id)
    if not triggers:
        _LOGGER.warning("ELAN doorbell: %s has no doorbell entity to watch", name)

    async def stream_url() -> str | None:
        cam = _protect_camera(hass, device)
        if cam is None:
            return None
        streams = await async_protect_streams(hass, cam.id)
        if not streams:
            _LOGGER.warning("ELAN doorbell: turn on RTSP for %s in Protect so ELAN gets its video", name)
            return None
        fit = [s for s in streams if s["width"] <= PREFERRED_LIVE_WIDTH]
        pick = max(fit, key=lambda s: s["width"]) if fit else min(streams, key=lambda s: s["width"])
        _LOGGER.debug("ELAN doorbell: sending %s", pick["label"])
        return _split_login(pick["url"])[0]

    async def talkback_target() -> dict | None:
        """Where and how the doorbell's speaker takes sound: Protect's talkback settings for the camera."""
        cam, api = _protect_camera(hass, device), _protect_api(hass)
        if cam is None or api is None:
            return None
        raw = await api.api_request_obj(f"cameras/{cam.id}")
        tb, flags = raw.get("talkbackSettings") or {}, raw.get("featureFlags") or {}
        if flags.get("hasSpeaker") is False or not raw.get("host"):
            _LOGGER.debug("ELAN doorbell: %s has no speaker to talk through", name)
            return None
        if (tb.get("typeFmt") or "aac") != "aac":
            _LOGGER.warning("ELAN doorbell: %s's speaker takes %s audio, which isn't supported yet", name, tb["typeFmt"])
            return None
        return {"host": raw["host"], "port": int(tb.get("bindPort") or 7004),
                "rate": int(tb.get("samplingRate") or 22050), "channels": int(tb.get("channels") or 1),
                "bitrate": int(tb.get("bitsPerSample") or 16) * 1000}

    from homeassistant.components.network import async_get_source_ip

    station = DoorStation(controller, await async_get_source_ip(hass), station_id, name,
                          media_source=stream_url, ffmpeg=_ffmpeg_binary(hass), talkback=talkback_target)
    await station.start()

    on_unload(station.close)

    @callback
    def on_press(event: Event) -> None:
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None or hass.state is not CoreState.running:     # starting or stopping
            return
        # Restarts and reloads recreate these entities (unavailable/unknown/restored values): only a real press counts.
        if new.domain == "binary_sensor":
            pressed = new.state == STATE_ON and old is not None and old.state == STATE_OFF
        else:   # event entity: its state is the time of the last event, so it must be a fresh one
            when = dt_util.parse_datetime(new.state)
            pressed = (old is not None and new.state != old.state and when is not None
                       and abs((dt_util.utcnow() - when).total_seconds()) < FRESH_PRESS
                       and new.attributes.get("event_type") == "ring")
        if pressed:
            _LOGGER.info("ELAN doorbell: %s pressed (%s)", name, new.entity_id)
            hass.async_create_task(station.ring())

    if triggers:
        on_unload(async_track_state_change_event(hass, triggers, on_press))
        _LOGGER.info("ELAN doorbell: %s rings ELAN on %s", name, ", ".join(triggers))

    async def ring_service(call: ServiceCall) -> None:
        await station.ring()

    hass.services.async_register(DOMAIN, SERVICE_RING, ring_service)
    on_unload(lambda: hass.services.async_remove(DOMAIN, SERVICE_RING))
    return station
