"""Camera analytics -> ELAN driver events. No Home Assistant imports here.

The HA Camera Events driver (type 9033 layout) can only raise the events its package declares, so each camera
sensor is mapped onto one of those. Several sensors can share an event (all the sound detections feed
"Audio Object Detected"): the event is On while any of them is on.
"""

from __future__ import annotations

import re
from typing import Optional

FIRST_EVENT_PORT = 10031
PULSE_SECONDS = 5           # event entities (doorbell ring, package) are momentary: On, then Off after this
AUDIO = "Audio Object Detected"

# binary_sensor label (normalized) -> ELAN event
BINARY = {
    "person_detected": "Person Detected",
    "vehicle_detected": "Vehicle Detected",
    "animal_detected": "Animal Detected",
    "package_detected": "Package Detected",
    "object_detected": "Object Detected",
    "baby_cry_detected": "Baby Cry Detected",
    "speaking_detected": "Speaking Detected",
    "co_alarm_detected": "CO Alarm Detected",
    "smoke_alarm_detected": "Smoke Alarm Detected",
    "barking_detected": AUDIO,
    "car_alarm_detected": AUDIO,
    "car_horn_detected": AUDIO,
    "glass_break_detected": AUDIO,
    "siren_detected": AUDIO,
    "motion": "Motion Detected",
    "doorbell": "Doorbell Detected",
    "is_dark": "Is Dark",
}
# event entity label -> (ELAN event pulsed or None, "Last ..." variable or None)
EVENT_ENTITIES = {
    "doorbell": ("Doorbell Detected", "Last Doorbell Ring"),
    "package": ("Package Detected", None),
    "fingerprint": (None, "Last Fingerprint Event"),
    "nfc": (None, "Last NFC Event"),
}
LAST_MOTION = "Last Motion Detected"


def assign_ports(devices: list, ports: dict) -> dict:
    ports = dict(ports)
    nxt = max([FIRST_EVENT_PORT - 1, *ports.values()]) + 1
    for device_id in devices:
        if device_id not in ports:
            ports[device_id] = nxt
            nxt += 1
    return ports


def normalize(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def classify(domain: str, labels: list, entity_id: str) -> Optional[tuple]:
    """('binary', event) or ('event', event_or_None, variable_or_None) for one entity, or None.

    `labels` are exact names to try first (the entity's original name / translation key); the entity id is
    matched by its ending as a fallback, longest key first so 'car_alarm_detected' wins over shorter keys.
    """
    table = BINARY if domain == "binary_sensor" else EVENT_ENTITIES if domain == "event" else None
    if table is None:
        return None
    for label in labels:
        key = normalize(label)
        if key in table:
            return _result(domain, table[key])
    object_id = normalize(entity_id.split(".", 1)[-1])
    for key in sorted(table, key=len, reverse=True):
        if object_id == key or object_id.endswith("_" + key):
            return _result(domain, table[key])
    return None


def _result(domain, value):
    return ("binary", value) if domain == "binary_sensor" else ("event", *value)


class EventStates:
    """Which sensors are on, per ELAN event; reports when an event's combined state changes."""

    def __init__(self) -> None:
        self.on: dict = {}          # event -> set of entity ids that are on

    def set(self, event: str, entity_id: str, is_on: bool) -> Optional[bool]:
        members = self.on.setdefault(event, set())
        before = bool(members)
        (members.add if is_on else members.discard)(entity_id)
        after = bool(members)
        return after if after != before else None
