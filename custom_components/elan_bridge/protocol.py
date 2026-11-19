"""The HA Bridge wire protocol: one text line per message, both ways.

Lighting driver (HA Bridge):
  Home Assistant -> ELAN: HELLO 1, PING, ADD <id> <type> <name>, STATE <id> ON|OFF, LEVEL <id> <0-100>, FORGET <id>
  ELAN -> Home Assistant: HELLO 1, PONG, ON <id>, OFF <id>, DIM <id> <0-100>, RAMP <id> UP|DOWN|STOP, ERR <text>
Media driver (HA Media Player), one connection per media player:
  Home Assistant -> ELAN: HELLO 1, PING, VAR <name> <text>, STATE <button> CHECK|UNCHECK|DISABLE
  ELAN -> Home Assistant: HELLO media 1, PONG, FUNC <button>, REPEAT <button>, STOP <button>, ACTIVE <true|false>

No Home Assistant imports here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PORT = 10010
VERSION = "1"
SWITCH = "SWITCH"
DIMMER = "DIMMER"


def _clean(text: str) -> str:
    """One line, printable, no leading/trailing spaces (the driver splits on whitespace and newlines)."""
    return " ".join(str(text).split())


def hello() -> str:
    return f"HELLO {VERSION}"


def ping() -> str:
    return "PING"


def add(dev_id: int, dev_type: str, name: str) -> str:
    name = _clean(name)[:60] or f"Device {dev_id}"
    return f"ADD {dev_id} {dev_type} {name}"


def state(dev_id: int, on: bool) -> str:
    return f"STATE {dev_id} {'ON' if on else 'OFF'}"


def level(dev_id: int, percent: int) -> str:
    return f"LEVEL {dev_id} {max(0, min(100, int(percent)))}"


def forget(dev_id: int) -> str:
    return f"FORGET {dev_id}"


def var(name: str, value: str) -> str:
    return f"VAR {name} {_clean(value)[:200]}"


def button_state(button: str, state: str) -> str:
    return f"STATE {button} {state}"


def camera_event(name: str, on: bool) -> str:
    return f"EVENT {name} {'On' if on else 'Off'}"


def named_var(name: str, value: str) -> str:
    """Camera-events driver variable: names contain spaces, so name and text are joined by '='."""
    return f"VAR {name}={_clean(value)[:200]}"


@dataclass(frozen=True)
class Command:
    """A message from ELAN."""
    verb: str                   # ON, OFF, DIM, RAMP, HELLO, PONG, ERR
    dev_id: Optional[int] = None
    value: Optional[str] = None  # DIM level, RAMP direction, ERR text


def parse(line: str) -> Optional[Command]:
    parts = line.strip().split(None, 2)
    if not parts:
        return None
    verb = parts[0].upper()
    if verb in ("HELLO", "PONG"):
        return Command(verb, value=parts[1] if len(parts) > 1 else None)
    if verb == "ERR":
        return Command(verb, value=line.strip()[4:])
    if verb in ("FUNC", "REPEAT", "STOP", "ACTIVE"):
        value = line.strip()[len(verb):].strip()
        return Command(verb, value=value) if value else None
    if verb in ("ARM", "DISARM", "STOPALARM", "BYPASS", "UNBYPASS") and len(parts) >= 2:
        try:                      # security driver: <verb> <partition|zone> [rest]
            return Command(verb, int(parts[1]), parts[2] if len(parts) > 2 else None)
        except ValueError:
            return None
    if verb in ("ON", "OFF", "DIM", "RAMP") and len(parts) >= 2:
        try:
            dev_id = int(parts[1])
        except ValueError:
            return None
        value = parts[2] if len(parts) > 2 else None
        if verb in ("DIM", "RAMP") and value is None:
            return None
        if verb == "DIM":
            try:
                value = str(max(0, min(100, int(float(value)))))
            except ValueError:
                return None
        return Command(verb, dev_id, value)
    return None


class LineBuffer:
    """Splits received bytes into lines."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list:
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        if len(self._buf) > 4096:     # never grows without a newline
            self._buf = b""
        return [ln.decode("utf-8", "replace").rstrip("\r") for ln in lines if ln.strip()]
