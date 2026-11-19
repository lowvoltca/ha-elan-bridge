"""Constants for the ELAN bridge integration."""

from __future__ import annotations

DOMAIN = "elan_bridge"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_ENTITIES = "entities"   # options: the entity ids shown in ELAN
CONF_IDS = "ids"             # options: entity id -> ELAN device number (TargetID), never reused
CONF_MEDIA = "media"         # options: media players shown as ELAN media sources
CONF_MEDIA_PORTS = "media_ports"  # options: media player -> port of its HA Media Player driver, never reused
CONF_CAMERAS = "cameras"     # options: camera device ids whose detections go to ELAN events
CONF_CAMERA_PORTS = "camera_ports"  # options: device id -> port of its HA Camera Events driver, never reused
CONF_NVR_CAMERAS = "nvr_cameras"     # options: Protect camera device ids served to ELAN's Viewer as an EL-NVR
CONF_SECURITY_SENSORS = "security_sensors"   # options: binary sensors shown as zones of ELAN's security partition
CONF_SECURITY_PANEL = "security_panel"       # options: optional alarm_control_panel armed/disarmed from ELAN
CONF_SECURITY_ZONES = "security_zones"       # options: entity id -> ELAN zone number (1+), never reused
CONF_DOORBELL = "doorbell"                   # options: Protect doorbell device that rings ELAN over SIP
CONF_STATION_ID = "station_id"               # data: this install's SIP station id (12 hex), made once at setup
SECURITY_PORT = 10040                        # the HA Security System driver's port in Configurator
CONF_NVR_LIVE = "nvr_live"           # options: device id -> live-view stream for ELAN ("auto", "protect:<channel>", "entity:<camera entity>")
CONF_NVR_PORTS = "nvr_ports"         # options: device id -> port of its virtual ONVIF camera (8001+), never reused

FIRST_ID = 101                  # 1 is taken by the driver test lamp
BRIDGED_DOMAINS = ["light", "switch", "fan", "input_boolean"]


def new_station_id() -> str:
    """A locally administered id ("02" + 10 random hex), so ELAN never mistakes it for one of its own doorbells."""
    import secrets
    return "02" + secrets.token_hex(5).upper()


def assign_ids(entities: list, ids: dict) -> dict:
    """Keep every number already given out (so ELAN's devices keep pointing at the same entity) and number new ones."""
    ids = dict(ids)
    next_id = max([FIRST_ID - 1, *ids.values()]) + 1
    for entity_id in entities:
        if entity_id not in ids:
            ids[entity_id] = next_id
            next_id += 1
    return ids
