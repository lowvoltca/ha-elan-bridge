"""Protect recordings in the ELAN Viewer: Home Assistant side of the EL-NVR emulator (see nvr_server.py).

Everything goes through Home Assistant's own UniFi Protect integration, so no Protect login or HA token is needed:
- each chosen camera device -> its Protect camera (by MAC, the way unifiprotect's own views do it);
- live view: the camera's Protect RTSP link (enable RTSP for at least one stream in Protect);
- recordings: Camera.get_video (Protect export), re-encoded with ffmpeg when keyframes are too big for the
  controller (Protect 4 MP+ streams) or the camera records H.265;
- timeline events: recorder history of the camera's Person / Vehicle / Motion binary sensors;
- thumbnails: Camera.get_snapshot at that time, shrunk to 640 px.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from . import events_logic as ev
from .nvr_server import NvrServer

_LOGGER = logging.getLogger(__name__)

FIRST_ONVIF_PORT = 8001
RETENTION_DAYS = 30
SENSOR_KIND = {"Person Detected": "person", "Vehicle Detected": "vehicle", "Motion Detected": "motion"}
PREFERRED_LIVE_WIDTH = 1280       # live view: the largest Protect RTSP stream up to this width (ELAN plays 960x720
                                  # doorbell streams live but not 1600x1200)


def assign_onvif_ports(devices: list, ports: dict) -> dict:
    """Keep ports already given out (ELAN's camera entries point at them) and number new devices from 8001."""
    ports = dict(ports)
    nxt = max([FIRST_ONVIF_PORT - 1, *ports.values()]) + 1
    for device_id in devices:
        if device_id not in ports:
            ports[device_id] = nxt
            nxt += 1
    return ports


def _protect_api(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries("unifiprotect"):
        data = getattr(entry, "runtime_data", None) or hass.data.get("unifiprotect", {}).get(entry.entry_id)
        api = getattr(data, "api", None)
        if api is not None:
            return api
    return None


def _live_link(cam) -> tuple:
    """(rtsp url, width, height) of the stream ELAN's live view should use, or (None, 0, 0)."""
    chans = [c for c in getattr(cam, "channels", []) if getattr(c, "is_rtsp_enabled", False)]
    if not chans:
        return None, 0, 0
    fit = [c for c in chans if (c.width or 0) <= PREFERRED_LIVE_WIDTH]
    ch = max(fit, key=lambda c: c.width or 0) if fit else min(chans, key=lambda c: c.width or 0)
    return getattr(ch, "rtsp_url", None), ch.width or 1920, ch.height or 1080


def _protect_camera(hass: HomeAssistant, device):
    """The Protect camera object for an HA device (matched by MAC, as unifiprotect's own views do), or None."""
    api = _protect_api(hass)
    if api is None or device is None:
        return None
    for kind, mac in device.connections:
        if kind == dr.CONNECTION_NETWORK_MAC:
            found = api.bootstrap.get_device_from_mac(mac)
            if found is not None and hasattr(found, "get_video"):
                return found
    return None


async def async_protect_streams(hass: HomeAssistant, protect_id: str) -> list:
    """Protect's RTSP streams for a camera: [{value, label, url, width, height}] (only streams with RTSP on)."""
    api = _protect_api(hass)
    if api is None:
        return []
    raw = await api.api_request_obj(f"cameras/{protect_id}")
    host = str(api.bootstrap.nvr.host)
    out = []
    for ch in raw.get("channels", []):
        if ch.get("isRtspEnabled") and ch.get("rtspAlias"):
            w, h = ch.get("width") or 0, ch.get("height") or 0
            out.append({"value": f"protect:{ch.get('id')}", "label": f"Protect {ch.get('name') or 'stream'} {w}x{h}",
                        "url": f"rtsp://{host}:7447/{ch['rtspAlias']}", "width": w, "height": h})
    return out


def other_camera_entities(hass: HomeAssistant, name: str) -> list:
    """Camera entities from other integrations (ONVIF, Hikvision...) on devices named like this camera,
    substreams first."""
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    want = ev.normalize(name)
    out = []
    for item in dev_reg.devices:      # newer HA yields entries, older HA yields ids
        device = item if hasattr(item, "id") else dev_reg.async_get(item)
        if device is None or ev.normalize(device.name_by_user or device.name or "") != want:
            continue
        for entry in er.async_entries_for_device(ent_reg, device.id):
            if entry.domain == "camera" and not entry.disabled_by and entry.platform != "unifiprotect":
                out.append(entry.entity_id)
    return sorted(out, key=lambda e: (0 if "sub" in e or "low" in e else 2 if "main" in e or "high" in e else 1, e))


async def async_live_choices(hass: HomeAssistant, device_id: str) -> list:
    """[{value, label}] for the options page's live-view stream picker of one NVR camera."""
    device = dr.async_get(hass).async_get(device_id)
    cam = _protect_camera(hass, device)
    choices = [{"value": "auto", "label": "Automatic (Protect stream up to 1280 wide, else the camera's substream)"}]
    if cam is not None:
        try:
            choices += [{"value": s["value"], "label": s["label"]} for s in await async_protect_streams(hass, cam.id)]
        except Exception as err:
            _LOGGER.debug("Protect streams for %s: %r", device_id, err)
    if device is not None:
        choices += [{"value": f"entity:{e}", "label": f"{e} (its own HA integration)"}
                    for e in other_camera_entities(hass, device.name_by_user or device.name or "")]
    return choices


def _split_login(url: str) -> tuple:
    """(url without user:password@, has_login). ELAN sends a camera's login from its Configurator page, not the link."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username and not parts.password:
        return url, False
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), True


class ProtectSource:
    """What NvrServer needs, answered from Home Assistant. Called from NvrServer's worker threads."""

    retention_days = RETENTION_DAYS

    def __init__(self, hass: HomeAssistant, store: Store, ffmpeg: str | None) -> None:
        self.hass = hass
        self.store = store
        self.ffmpeg = ffmpeg
        self._cams: dict = {}        # key (device id) -> {"protect_id", "sensors": {entity: kind}, ...}
        self._hist: dict = {}
        self._hist_lock = threading.Lock()

    # ---- built on the event loop at setup
    def resolve(self, devices: list, ports: dict) -> None:
        api = _protect_api(self.hass)
        if api is None:
            _LOGGER.error("ELAN NVR: the UniFi Protect integration isn't loaded")
            return
        dev_reg, ent_reg = dr.async_get(self.hass), er.async_get(self.hass)
        for device_id in devices:
            device = dev_reg.async_get(device_id)
            if device is None:
                continue
            cam = _protect_camera(self.hass, device)
            name = device.name_by_user or device.name or device_id
            if cam is None:
                _LOGGER.warning("ELAN NVR: %s is not a UniFi Protect camera", name)
                continue
            link, width, height = _live_link(cam)
            sensors, cam_entities = {}, []
            for entry in er.async_entries_for_device(ent_reg, device_id):
                kind = ev.classify(entry.domain, [entry.original_name, entry.translation_key], entry.entity_id)
                if kind and kind[0] == "binary" and kind[1] in SENSOR_KIND and not entry.disabled_by:
                    sensors[entry.entity_id] = SENSOR_KIND[kind[1]]
                if entry.domain == "camera" and not entry.disabled_by:
                    cam_entities.append(entry.entity_id)
            self._cams[device_id] = {"key": device_id, "name": name, "protect_id": cam.id, "rtsp": link or "",
                                     "width": width, "height": height, "port": ports[device_id], "sensors": sensors,
                                     "entities": cam_entities}

    async def async_fill_live_links(self, live: dict) -> None:
        """Pick each camera's live-view link: the stream chosen on the options page, else automatic (Protect's
        largest stream up to 1280 wide; for a camera Protect doesn't re-share, its own HA integration's substream)."""
        from homeassistant.components.camera import async_get_stream_source

        async def entity_url(eid):
            try:
                src = await async_get_stream_source(self.hass, eid)
            except Exception as err:
                _LOGGER.debug("stream source %s: %r", eid, err)
                return ""
            if src and src.startswith("rtsp"):
                return _plain_rtsp(src) if src.startswith("rtsps://") else src
            return ""

        for c in self._cams.values():
            choice = live.get(c["key"], "auto")
            streams = []
            try:
                streams = await async_protect_streams(self.hass, c["protect_id"])
            except Exception as err:
                _LOGGER.warning("ELAN NVR: reading Protect streams of %s failed: %r", c["name"], err)
            picked = None
            if choice.startswith("protect:"):
                picked = next((st for st in streams if st["value"] == choice), None)
            elif choice.startswith("entity:"):
                url = await entity_url(choice[len("entity:"):])
                picked = {"url": url, "width": 0, "height": 0} if url else None
            if picked is None and choice != "auto":
                _LOGGER.warning("ELAN NVR: the live stream chosen for %s isn't available; using automatic", c["name"])
            if picked is None and streams:
                fit = [st for st in streams if st["width"] <= PREFERRED_LIVE_WIDTH]
                picked = max(fit, key=lambda st: st["width"]) if fit else min(streams, key=lambda st: st["width"])
            if picked is None and c["rtsp"]:
                picked = {"url": c["rtsp"], "width": c["width"], "height": c["height"]}
            if picked is None:
                for eid in other_camera_entities(self.hass, c["name"]):
                    url = await entity_url(eid)
                    if url:
                        picked = {"url": url, "width": 0, "height": 0}
                        break
            if picked is None:
                c["rtsp"] = ""
                _LOGGER.warning("ELAN NVR: %s has no live stream (turn on RTSP in Protect, or add the camera to HA "
                                "with its own integration)", c["name"])
                continue
            c["rtsp"], has_login = _split_login(picked["url"])
            c["width"], c["height"] = (picked["width"], picked["height"]) if picked["width"] else (1280, 720)
            _LOGGER.info("ELAN NVR: %s live view -> %s (%sx%s)", c["name"], c["rtsp"].split("/")[2],
                         c["width"], c["height"])
            if has_login:
                _LOGGER.warning("ELAN NVR: %s's live stream needs the camera's login. Enter that username and password "
                                "on the camera's page in ELAN Configurator (Video Cameras -> %s)", c["name"], c["name"])

    # ---- NvrServer interface
    def cameras(self) -> list:
        return [dict(c) for c in self._cams.values()]

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.hass.loop).result(timeout)

    def _camera(self, key: str):
        api = _protect_api(self.hass)
        return api.bootstrap.cameras.get(self._cams[key]["protect_id"]) if api else None

    def video(self, key: str, start: float, end: float) -> bytes | None:
        cam = self._camera(key)
        if cam is None:
            return None
        utc = dt.timezone.utc
        t0 = time.time()
        data = self._run(cam.get_video(dt.datetime.fromtimestamp(start, utc), dt.datetime.fromtimestamp(end, utc)), 90)
        _LOGGER.debug("Protect export %s %ss: %s bytes in %.1fs", self._cams[key]["name"], int(end - start),
                      len(data or b""), time.time() - t0)
        return data

    def snapshot(self, key: str, epoch: float | None) -> bytes | None:
        cam = self._camera(key)
        if cam is None:
            return None
        when = dt.datetime.fromtimestamp(epoch, dt.timezone.utc) if epoch else None
        jpg = self._run(cam.get_snapshot(dt=when) if when else cam.get_snapshot(), 20)
        return _shrink(jpg, 640) if jpg else None

    def detections(self, key: str, start: float, end: float) -> list:
        sensors = self._cams.get(key, {}).get("sensors", {})
        if not sensors:
            return []
        ck = (key, int(start) // 3600, int(end) // 3600)
        with self._hist_lock:
            hit = self._hist.get(ck)
            if hit and time.time() - hit[0] < (60 if end > time.time() - 3600 else 3600):
                return hit[1]
        from homeassistant.components.recorder import get_instance, history

        utc = dt.timezone.utc
        s, e = dt.datetime.fromtimestamp(start, utc), dt.datetime.fromtimestamp(end, utc)
        out = []
        async def _fetch(eid):   # the recorder's executor must be used from the event loop
            return await get_instance(self.hass).async_add_executor_job(
                lambda: history.state_changes_during_period(
                    self.hass, s, e, eid, no_attributes=True, include_start_time_state=True))

        for entity_id, kind in sensors.items():
            try:
                rows = self._run(_fetch(entity_id), 30)
            except Exception as err:
                _LOGGER.warning("ELAN NVR: history of %s failed: %r", entity_id, err)
                continue
            prev = None
            for state in rows.get(entity_id, []):
                when = state.last_changed.timestamp()
                if state.state == "on" and prev not in (None, "on") and start <= when < end:
                    out.append((int(when), kind))
                prev = state.state
        out.sort()
        with self._hist_lock:
            self._hist[ck] = (time.time(), out)
        return out

    def downscale(self, mp4: bytes, seconds: float) -> bytes | None:
        """640 wide H.264, no B-frames, a keyframe every second: fits the controller's frame buffers."""
        if not self.ffmpeg:
            _LOGGER.warning("ELAN NVR: ffmpeg not found; recordings with big keyframes won't play in ELAN")
            return None
        t0 = time.time()
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp4")
            with open(src, "wb") as f:
                f.write(mp4)
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-an", "-t", str(seconds),
                   "-vf", "scale=640:-2", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
                   "-bf", "0", "-force_key_frames", "expr:gte(t,n_forced*1)", "-sc_threshold", "0",
                   "-b:v", "1200k", "-maxrate", "1500k", "-bufsize", "1500k", "-pix_fmt", "yuv420p", dst]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode != 0:
                _LOGGER.warning("ELAN NVR: ffmpeg failed: %s", r.stderr.decode(errors="replace")[-300:])
                return None
            with open(dst, "rb") as f:
                out = f.read()
        _LOGGER.debug("re-encoded %s KB -> %s KB in %.1fs", len(mp4) // 1000, len(out) // 1000, time.time() - t0)
        return out

    def save_channels(self, channels: dict) -> None:
        data = {str(k): v for k, v in channels.items()}
        self.hass.loop.call_soon_threadsafe(self.store.async_delay_save, lambda: data, 2)


def _plain_rtsp(url: str) -> str:
    """HA hands out Protect's RTSPS link (port 7441, ?enableSrtp); ELAN plays plain RTSP on 7447."""
    if url.startswith("rtsps://"):
        url = "rtsp://" + url[len("rtsps://"):].replace(":7441/", ":7447/", 1)
    return url.split("?", 1)[0]


def _shrink(jpg: bytes, width: int) -> bytes:
    """Protect returns full-resolution JPEGs (about 1.5 MB at 4 MP); ELAN shows thumbnails."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(jpg))
        if im.width > width:
            im = im.resize((width, im.height * width // im.width))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return jpg


def _ffmpeg_binary(hass: HomeAssistant) -> str | None:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:
        return shutil.which("ffmpeg")


async def async_setup_nvr(hass: HomeAssistant, entry_id: str, devices: list, ports: dict, on_unload,
                          live: dict | None = None):
    """Start the EL-NVR emulator for the chosen Protect camera devices; returns the server (or None)."""
    if not devices:
        return None
    from homeassistant.components.network import async_get_source_ip

    ip = await async_get_source_ip(hass)
    store = Store(hass, 1, f"elan_bridge.{entry_id}.nvr_channels")
    channels = await store.async_load() or {}
    source = ProtectSource(hass, store, _ffmpeg_binary(hass))
    source.resolve(devices, ports)
    await source.async_fill_live_links(live or {})
    server = NvrServer(source, ip, channels)
    await hass.async_add_executor_job(server.start)
    on_unload(server.stop)
    return server
