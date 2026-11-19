"""Media players: what ELAN's media page shows, and what each page button does. No Home Assistant imports here.

Button names are the media page's function list; page variables are the ones its layout binds:
currentTitle (Now Playing), currentSource, currentCover (artwork URL), currentPosition.
"""

from __future__ import annotations

from typing import Optional

from . import protocol as p

FIRST_MEDIA_PORT = 10021

# Buttons that are plain media_player services.
MEDIA_SERVICES = {
    "Play": "media_play",
    "Pause": "media_pause",
    "Play/Pause": "media_play_pause",
    "Stop": "media_stop",
    "Next": "media_next_track",
    "Skip Forward": "media_next_track",
    "Previous": "media_previous_track",
    "Skip Back": "media_previous_track",
    "Volume Up": "volume_up",
    "Volume Down": "volume_down",
    "Power Cycle": "toggle",
    "Toggle": "toggle",
    "Turn On": "turn_on",
    "Turn Off": "turn_off",
}
MUTE_BUTTONS = ("Mute Toggle", "Volume Mute")
REPEATING = ("Volume Up", "Volume Down")       # held down: keep going

# Navigation goes to the remote entity on the same device (Apple TV, Roku, Android TV, ...).
REMOTE_KEYS = ("Up", "Down", "Left", "Right", "Enter", "Home", "Menu")
REMOTE_COMMANDS = {
    # remote platform -> ELAN button -> command name that integration's remote.send_command understands
    "androidtv_remote": {"Up": "DPAD_UP", "Down": "DPAD_DOWN", "Left": "DPAD_LEFT", "Right": "DPAD_RIGHT",
                         "Enter": "DPAD_CENTER", "Home": "HOME", "Menu": "BACK"},
    "default": {"Up": "up", "Down": "down", "Left": "left", "Right": "right",
                "Enter": "select", "Home": "home", "Menu": "menu"},   # apple_tv, roku and most others
}


def assign_ports(entities: list, ports: dict) -> dict:
    """Keep every port already given out (ELAN's sources are set to them) and give new players the next ones."""
    ports = dict(ports)
    nxt = max([FIRST_MEDIA_PORT - 1, *ports.values()]) + 1
    for entity_id in entities:
        if entity_id not in ports:
            ports[entity_id] = nxt
            nxt += 1
    return ports


def _clock(seconds) -> str:
    seconds = int(max(0, float(seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def page_values(state: str, attrs: dict, cover_base: str = "") -> dict:
    """Page variable -> text, from a media_player's state and attributes."""
    name = attrs.get("friendly_name", "")
    title = attrs.get("media_title") or ""
    artist = attrs.get("media_artist") or attrs.get("media_series_title") or ""
    if state in ("off", "unavailable", "unknown"):
        now = f"{name}: {'off' if state == 'off' else 'unavailable'}"
    elif title and artist:
        now = f"{artist} - {title}"
    else:
        now = title or artist or state.capitalize()
    source = attrs.get("app_name") or attrs.get("source") or name
    picture = attrs.get("entity_picture") or ""
    if picture.startswith("/"):
        picture = cover_base.rstrip("/") + picture if cover_base else ""
    position = ""
    if attrs.get("media_duration"):
        position = f"{_clock(attrs.get('media_position') or 0)} / {_clock(attrs['media_duration'])}"
    return {"currentTitle": now, "currentSource": source, "currentCover": picture, "currentPosition": position}


def button_states(state: str) -> dict:
    playing = state == "playing"
    return {
        "Play": "CHECK" if playing else "UNCHECK",
        "Pause": "CHECK" if state == "paused" else "UNCHECK",
        "Play/Pause": "CHECK" if playing else "UNCHECK",
    }


def page_lines(values: dict, states: dict, sent: dict) -> list:
    """Protocol lines for what changed since `sent` (updated in place). Keeps traffic to the controller low."""
    lines = []
    for name, value in values.items():
        if sent.get(("var", name)) != value:
            sent[("var", name)] = value
            lines.append(p.var(name, value))
    for button, st in states.items():
        if sent.get(("state", button)) != st:
            sent[("state", button)] = st
            lines.append(p.button_state(button, st))
    return lines


def action(button: str, attrs: dict, remote_entity: Optional[str], remote_platform: Optional[str]):
    """(domain, service, data-without-entity, target) for a page button, or None."""
    if button in MEDIA_SERVICES:
        return ("media_player", MEDIA_SERVICES[button], {}, "media")
    if button in MUTE_BUTTONS:
        return ("media_player", "volume_mute", {"is_volume_muted": not attrs.get("is_volume_muted", False)}, "media")
    if button in REMOTE_KEYS and remote_entity:
        table = REMOTE_COMMANDS.get(remote_platform or "", REMOTE_COMMANDS["default"])
        return ("remote", "send_command", {"command": table[button]}, "remote")
    return None
