"""ELAN EL-NVR emulator: UniFi Protect recordings in the ELAN Viewer. No Home Assistant imports here.

Pretends to be an ELAN EL-NVR-8CH so the controller's own EL-NVR driver talks to it,
plus one virtual ONVIF camera per Protect camera (the EL-NVR only offers ONVIF-family cameras on its channels;
GetStreamUri hands back the Protect RTSP link, so live view goes straight from Protect to ELAN).

All Protect/HA access goes through `source` (see nvr.ProtectSource), called from worker threads:
  source.cameras()                  -> [{"key", "name", "rtsp", "width", "height", "port"}]
  source.video(key, start, end)     -> MP4 bytes (Protect export; t=0 is the keyframe at/before start)
  source.snapshot(key, epoch|None)  -> JPEG bytes (small)
  source.detections(key, start, end)-> [(epoch, "person"|"vehicle"|"motion")]
  source.downscale(mp4, seconds)    -> MP4 bytes, 640 wide, no B-frames, keyframe every 1 s (or None)
  source.save_channels(dict)        ;  source.retention_days

Threads + blocking sockets, like the tested stand-alone emulator; stop() closes every socket.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import re
import socket
import struct
import threading
import time
import urllib.parse
import uuid

from . import nvr_mp4

_LOGGER = logging.getLogger(__name__)

SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
WSD_ADDR, WSD_PORT = "239.255.255.250", 3702
SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
FW = "9.9.99"     # >= 1.0.34 enables streaming; above ELAN's online EL-NVR list avoids "Firmware Update Available"
CHANNELS = 8

CLIP = 10         # seconds per video piece (a multiple of Protect's usual 5 s keyframe spacing)
GOP = 5
# Controller RX buffers: 512,000 bytes for playback, 256,000 for the seek preview. A bigger packet reads as "stream
# ended" and the controller reconnects in a loop, so pieces with bigger keyframes (Protect 4 MP+) are re-encoded.
KEY_LIMIT = 200_000
AUD = b"\x00\x00\x00\x01\x09\xf0"     # access unit delimiter: keep-alive while a piece is fetched
# ELAN knows only these kinds (anything else is dropped). It has no doorbell kind, so rings use the otherwise
# unused "entered" intrusion kind and get their own mark on the timeline.
TYPE_NAME = {"motion": "motion", "person": "ivsintrusion_person", "vehicle": "ivsintrusion_vehicle",
             "ring": "ivsintrusion_entered"}
FF_SPEED = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16}
STREAM_PREFIXES = ("getpbthumbnail", "gethdpbstream", "getpbstream")
IMAGE_PREFIXES = ("timeline?cmd=jpeg", "snap")
WRITE_REPLY_TAGS = [("camerasetup", "CameraGeneralResponse"), ("submenu=schedule", "RecordScheduleResponse"),
                    ("recordsetup", "RecordGeneralResponse"), ("alarmsetup", "AlarmGeneralResponse"),
                    ("metadatasetup", "MetadataGeneralResponse"), ("submenu=user", "SystemUserResponse"),
                    ("networksetup", "NetworkGeneralResponse")]


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def packet(frame: bytes, ts: float, ch: int) -> bytes:
    """24-byte "WW" header + one whole frame."""
    h = bytearray(24)
    h[0:2] = b"WW"
    struct.pack_into("<I", h, 4, 24 + len(frame))
    struct.pack_into("<I", h, 9, int(ts))
    h[17] = ch
    return bytes(h) + frame


def parse_channel_writes(text: str) -> dict:
    """{0-based ch: fields} from `camerasetup…&cmd=setup&detail=(;ch:0nsbp;key:valnsbp;…)(…)`, minus credentials."""
    out = {}
    for group in re.findall(r"\(;([^)]*)\)", text):
        f = {k: v for k, v in (kv.split(":", 1) for kv in group.split("nsbp;") if ":" in kv)
             if k not in ("pwd", "username")}
        if f.get("ch", "").isdigit():
            out[int(f["ch"])] = f
    return out


class NvrServer:
    def __init__(self, source, ip: str, channels: dict, http_port: int = 80) -> None:
        self.source = source
        self.ip = ip
        self.port = http_port
        self.channels = {int(k): v for k, v in (channels or {}).items()}
        self.cams: list = []
        self.udn = "uuid:" + str(uuid.uuid5(uuid.NAMESPACE_DNS, f"ha-elan-nvr-{ip}"))
        self._socks: list = []
        self._stop = threading.Event()
        self._clips: collections.OrderedDict = collections.OrderedDict()
        self._clip_lock = threading.Lock()
        self._mapped: dict = {}
        self.trace: collections.deque = collections.deque(maxlen=3000)   # served at /ha_trace

    def _t(self, msg: str) -> None:
        self.trace.append(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}")

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.cams = self.source.cameras()
        self._serve_tcp(self.port, self._handle, "EL-NVR HTTP")
        for c in self.cams:
            c["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "ha-onvif/" + c["key"]))
            self._serve_tcp(c["port"], lambda conn, addr, c=c: self._onvif_conn(conn, addr, c), f"ONVIF {c['name']}")
        self._thread(self._ssdp, "SSDP")
        self._thread(self._ws_discovery, "WS-Discovery")
        _LOGGER.info("ELAN NVR emulator on %s:%s with %d virtual ONVIF cameras", self.ip, self.port, len(self.cams))

    def stop(self) -> None:
        self._stop.set()
        for s in self._socks:
            try:
                s.shutdown(socket.SHUT_RDWR)   # Linux: wakes a thread blocked in accept()/recv(); close() alone
            except OSError:                    # leaves the port bound until that call returns
                pass
            try:
                s.close()
            except OSError:
                pass
        self._socks.clear()

    def _thread(self, target, name: str) -> None:
        def run():
            try:
                target()
            except OSError as e:
                if not self._stop.is_set():
                    _LOGGER.warning("%s stopped: %s", name, e)
        threading.Thread(target=run, name=f"elan_nvr {name}", daemon=True).start()

    def _serve_tcp(self, port: int, handler, name: str) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for attempt in range(20):            # a reload's previous server may still be letting go of the port
            try:
                s.bind(("", port))
                break
            except OSError as e:
                if attempt == 19:
                    s.close()
                    _LOGGER.error("%s: port %s is taken (%s)", name, port, e)
                    return
                time.sleep(0.5)
        s.listen(32)
        self._socks.append(s)

        def accept_loop():
            while not self._stop.is_set():
                conn, addr = s.accept()
                if self._stop.is_set():
                    conn.close()
                    return
                threading.Thread(target=handler, args=(conn, addr), daemon=True).start()
        self._thread(accept_loop, name)

    def _udp_listener(self, group: str, port: int):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(self.ip))
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        out.bind((self.ip, 0))
        out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.ip))
        out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self._socks += [s, out]
        return s, out

    # ------------------------------------------------------------ discovery
    def _ssdp_headers(self, st: str) -> str:
        usn = self.udn if st == self.udn else f"{self.udn}::{st}"
        return (f"CACHE-CONTROL: max-age=1800\r\nLOCATION: http://{self.ip}:{self.port}/description.xml\r\n"
                f"SERVER: Linux/3.0 UPnP/1.0 ELAN-NVR/1.0.34\r\nST: {st}\r\nUSN: {usn}\r\n")

    def _ssdp(self) -> None:
        s, out = self._udp_listener(SSDP_ADDR, SSDP_PORT)
        targets = ("upnp:rootdevice", self.udn, "urn:schemas-upnp-org:device:Basic:1")

        def notify():
            while not self._stop.is_set():
                for st in targets:
                    msg = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nNTS: ssdp:alive\r\n"
                           + self._ssdp_headers(st).replace("ST: ", "NT: ") + "\r\n")
                    try:
                        out.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
                    except OSError:
                        return
                self._stop.wait(60)
        self._thread(notify, "SSDP notify")
        while not self._stop.is_set():
            data, src = s.recvfrom(4096)
            if not src:                      # socket shut down by stop(): recvfrom returns (b'', None)
                return
            text = data.decode("latin-1", "replace")
            if not text.startswith("M-SEARCH") or src[0] == self.ip:
                continue
            st = next((h[3:].strip() for h in text.split("\r\n") if h.lower().startswith("st:")), "")
            sts = targets if st == "ssdp:all" else (st,) if st in targets else ()
            for t in sts:
                out.sendto(("HTTP/1.1 200 OK\r\nEXT:\r\n" + self._ssdp_headers(t) + "\r\n").encode(), src)

    def _ws_discovery(self) -> None:
        if not self.cams:
            return
        s, out = self._udp_listener(WSD_ADDR, WSD_PORT)
        while not self._stop.is_set():
            data, src = s.recvfrom(65536)
            if not src:                      # socket shut down by stop(): recvfrom returns (b'', None)
                return
            if src[0] == self.ip or b"Probe" not in data or b"ProbeMatch" in data:
                continue
            m = re.search(rb"MessageID[^>]*>\s*([^<\s]+)\s*<", data)
            mid = m.group(1).decode() if m else "uuid:0"
            for c in self.cams:
                out.sendto(self._probe_match(c, mid).encode(), src)

    # ------------------------------------------------------------ EL-NVR HTTP
    def _description(self) -> str:
        return (f'<?xml version="1.0"?>\n<root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion><major>1</major>'
                f"<minor>0</minor></specVersion><URLBase>http://{self.ip}:{self.port}</URLBase><device>"
                "<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType><friendlyName>HA NVR</friendlyName>"
                "<manufacturer>ELAN</manufacturer><modelDescription>Network Video Recorder</modelDescription>"
                "<modelName>EL-NVR-8CH</modelName><modelNumber>EL-NVR-8CH</modelNumber><serialNumber>HA0001"
                f"</serialNumber><UDN>{self.udn}</UDN><presentationURL>http://{self.ip}:{self.port}/</presentationURL>"
                "</device></root>\n")

    def _camera_general(self) -> str:
        """Echo back what the controller wrote, or it re-pushes the setup every 6 s (hostname mismatch check)."""
        def one(i):
            f = self.channels.get(i, {})
            res = "Single Stream SD" if "single" in f.get("resolution", "") else "Dual Stream"
            mode = "Smooth" if f.get("decodermode") == "smooth" else "Immediate"
            return (f"<Channel{i}><Source>IP Camera</Source><Hostname>{f.get('hostname', '0.0.0.0')}</Hostname>"
                    f"<Resolution>{res}</Resolution><PrimaryFPS>{f.get('primaryfps', 15)}</PrimaryFPS>"
                    f"<PrimaryBitrate>{f.get('primarybitrate', 4096)}</PrimaryBitrate>"
                    f"<SecondaryFPS>{f.get('secondaryfps', 15)}</SecondaryFPS>"
                    f"<SecondaryBitrate>{f.get('secondarybitrate', 1024)}</SecondaryBitrate>"
                    f"<DecoderMode>{mode}</DecoderMode></Channel{i}>")
        return ("<CameraGeneralResponse><MaxTotalBitrate>80000</MaxTotalBitrate><MaxPrimaryFPS>30</MaxPrimaryFPS>"
                f"<MaxSecondaryFPS>30</MaxSecondaryFPS>{''.join(one(i) for i in range(CHANNELS))}"
                "</CameraGeneralResponse>")

    def _camera_page(self, c: str) -> str | None:
        """Settings pages ELAN's ONVIF camera driver reads from an ELAN-brand camera's web server (here: the virtual
        camera's address, so this server). It parses `var X = GV('value'` lines inside <script> and retries about
        three times a second while it can't; 2026-11-15 that retry flood (700 requests in 4 min) preceded the SC-100
        going down. Plain values: no mirror/flip, no privacy mask, static address = this server's."""
        def script(*pairs):
            return "<html><head><script>\n" + "".join(f"var {k} = GV('{v}','{v}');\n" for k, v in pairs) + \
                   "</script></head><body></body></html>"
        if c.startswith("new/"):              # a loop like 2026-11-15 shows up in the log, once per hour at most
            now = time.time()
            self._page_hits = [t for t in getattr(self, "_page_hits", []) if now - t < 60] + [now]
            if len(self._page_hits) > 30 and now - getattr(self, "_page_warned", 0) > 3600:
                self._page_warned = now
                _LOGGER.warning("ELAN keeps re-reading camera settings pages (/%s, %d a minute)", c[:40],
                                len(self._page_hits))
        if c.startswith("new/video_quailty2.htm"):
            return script(("MIRRORvalue", 0), ("FLIPvalue", 0))
        if c.startswith("new/video_mask.htm"):
            return script(("g_privacy_enable", 0))
        if c.startswith("new/net_general.htm"):
            gw = ".".join(self.ip.split(".")[:3] + ["1"])
            return script(("g_eth_proto", 0), ("g_ipaddr", self.ip), ("g_eth_netmask", "255.255.255.0"),
                          ("g_eth_gateway", gw), ("g_eth_dns1", gw))
        return None

    def reply_for(self, cmd: str) -> tuple:
        """(content-type, body) for a non-stream command; cmd is the path without the leading '/'."""
        c = cmd.lower().lstrip("./")
        page = self._camera_page(c)
        if page is not None:
            return "text/html", page
        if c.startswith("description.xml"):
            return "text/xml", self._description()
        if c.startswith("server"):
            return "text/plain", f"Model=313\r\nSoftware Version={FW}\r\nHardware Version=1.0\r\n"
        if c.startswith("status?cmd=getsystemstatus"):
            return "text/xml", ("<GetSystemStatusResponse><EnvironmentStatus><Fan>ok</Fan></EnvironmentStatus>"
                                "<HardDrivesStatus><MaxHardDriveNumber>1</MaxHardDriveNumber><HardDrive01><Capacity>"
                                "4000000</Capacity><Status>ok</Status></HardDrive01></HardDrivesStatus>"
                                "</GetSystemStatusResponse>")
        if c.startswith("status?cmd=getcamerastatus"):
            cams = "".join(f"<Camera{i:02d}><SourceHealth>connected</SourceHealth></Camera{i:02d}>"
                           for i in range(1, CHANNELS + 1))
            return "text/xml", (f"<GetCameraStatusResponse><CameraStatus><MaxCameraNumber>{CHANNELS}</MaxCameraNumber>"
                                f"{cams}</CameraStatus></GetCameraStatusResponse>")
        if "cmd=" not in c and c.startswith("camerasetup?submenu=general"):
            return "text/xml", self._camera_general()
        if c == "recordsetup?submenu=general":
            return "text/xml", ("<RecordGeneralResponse><PostalarmRec>10</PostalarmRec><WeightedRec>On</WeightedRec>"
                                "</RecordGeneralResponse>")
        if c == "recordsetup?submenu=schedule":
            return "text/xml", "<RecordScheduleResponse></RecordScheduleResponse>"
        if c == "alarmsetup?submenu=general":
            return "text/xml", "<AlarmGeneralResponse></AlarmGeneralResponse>"
        if c.startswith(("backup?backup?cmd=query", "backup?cmd=query")):
            return "text/xml", self.days()
        if c.startswith("timeline?cmd=data"):
            q = urllib.parse.parse_qs(cmd.split("?", 1)[1])
            return "application/json", self.timeline(int(q["start"][0]), int(q["end"][0]))
        if c.startswith("eventindex_file"):
            q = dict(kv.split("=", 1) for kv in cmd.split("&")[1:] if "=" in kv)
            return "text/xml", self.event_list(q["from"], q["to"])
        for menu, tag in WRITE_REPLY_TAGS:
            if menu in c:
                # Writes are checked for <Status>OK</Status> in the menu's tag; a bare OK makes the driver retry.
                return "text/xml", f"<{tag}><Status>OK</Status></{tag}>"
        return "text/plain", "OK"

    def _handle(self, conn, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        try:
            conn.settimeout(30)
            line, headers, body = _read_request(conn)
            if line is None:
                return
            method, target = (line.split(" ") + ["", ""])[:2]
            cmd = urllib.parse.unquote(target.lstrip("/"))
            low = cmd.lower()
            _LOGGER.debug("%s %s /%s", peer, method, cmd[:120])
            if low.startswith("ha_trace"):
                payload = "\n".join(list(self.trace)).encode()
                conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\n"
                             "Connection: close\r\n\r\n".encode() + payload)
                return
            self._t(f"{peer} {method} /{cmd[:150]}" + (f" body={body[:150]!r}" if body else ""))
            if low.startswith(STREAM_PREFIXES):
                self._stream(conn, peer, cmd, body)
                return
            if low.startswith(IMAGE_PREFIXES):
                jpg = self.jpeg(cmd)
                conn.sendall((f"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n"
                              "Connection: close\r\n\r\n").encode() + jpg if jpg else
                             b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if method == "POST" and body:
                cmd = body.decode("latin-1").strip()     # setup writes carry the real command in the body
            if "camerasetup?submenu=general&cmd=setup" in cmd:
                self._remember(cmd)
            ctype, text = self.reply_for(cmd)
            payload = text.encode()
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(payload)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + payload)
        except Exception as e:     # one bad request must not take the server down
            _LOGGER.debug("%s: %r", peer, e)
        finally:
            conn.close()

    def _remember(self, cmd: str) -> None:
        changed = False
        for ch, f in parse_channel_writes(cmd).items():
            if self.channels.get(ch) != f:
                self.channels[ch] = f
                changed = True
        if changed:
            self.source.save_channels(dict(self.channels))

    # ------------------------------------------------------------ channel -> camera
    def cam_for(self, ch: int):
        f = self.channels.get(ch)
        # an unused channel is source "off"; the hostname can be 0.0.0.0 even for a real camera (the controller
        # fills it from the camera's stream link, which it may not have yet), so the camera name decides
        if not f or f.get("source", "").lower() == "off" or not norm(f.get("camname")):
            return None
        if f.get("hostname", "0.0.0.0") == "0.0.0.0" and re.fullmatch(r"cam\d+", norm(f.get("camname"))):
            return None     # an empty channel keeps the controller's placeholder name (CAM4 ...)
        name = norm(f.get("camname"))
        cam = next((c for c in self.cams if norm(c["name"]) == name), None)
        if cam is None:      # tolerate a renamed ELAN camera that still contains the Protect name (or vice versa)
            cam = next((c for c in self.cams if norm(c["name"]) and (norm(c["name"]) in name or name in norm(c["name"]))),
                       None)
        if self._mapped.get(ch, "?") != (cam and cam["key"]):
            self._mapped[ch] = cam and cam["key"]
            if cam:
                _LOGGER.info("EL-NVR channel %d (%s) -> %s", ch + 1, f.get("camname"), cam["name"])
            else:
                _LOGGER.warning("EL-NVR channel %d: ELAN camera '%s' matches no Protect camera name; name the ELAN "
                                "camera like the Protect camera", ch + 1, f.get("camname"))
        return cam

    def used(self) -> list:
        return [(ch, c) for ch in sorted(self.channels) if (c := self.cam_for(ch))]

    # ------------------------------------------------------------ timeline / events / images
    def _window(self, start: float, end: float) -> tuple:
        now = time.time()
        return max(start, now - self.source.retention_days * 86400), min(end, now)

    def days(self) -> str:
        today = dt.datetime.now(dt.timezone.utc).date()
        return "".join(f"<Day>{today - dt.timedelta(days=k):%Y-%m-%d}</Day>"
                       for k in range(self.source.retention_days, -1, -1))

    def timeline(self, start: int, end: int) -> str:
        """JSON for timeline?cmd=data. Channel numbers are 0-based here (confirmed live); every value a string."""
        s, e = self._window(start, end)
        out: list = []
        if s >= e:
            return "[]"
        for ch, cam in self.used():
            t = int(s) - int(s) % 600
            while t < e:
                out.append({"type": "rec", "channel": str(ch), "from": str(t)})
                t += 600
            for when, kind in self.source.detections(cam["key"], s, e):
                out.append({"type": TYPE_NAME.get(kind, "motion"), "channel": str(ch), "from": str(when),
                            "image": "timeline?cmd=jpeg&hdd=0"})
        return json.dumps(out, separators=(",", ":"))

    def event_list(self, day_from: str, day_to: str) -> str:
        s = dt.datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
        e = dt.datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() + 86400
        s, e = self._window(s, e)
        evs = []
        for ch, cam in self.used():
            evs += [(t, ch) for t, _ in self.source.detections(cam["key"], s, e)]
        evs = sorted(evs, reverse=True)[:500]
        body = "".join(f"<Event{i}><EventTime>{dt.datetime.fromtimestamp(t, dt.timezone.utc):%Y-%m-%d %H:%M:%S}"
                       f"</EventTime><EventChannel>{ch}</EventChannel></Event{i}>"
                       for i, (t, ch) in enumerate(evs, start=1))
        return f"<ESearchMax>{len(evs)}</ESearchMax>{body}"

    def jpeg(self, cmd: str):
        try:
            if cmd.lower().startswith("snap"):
                cam = self.cam_for(int(re.match(r"snap(\d+)", cmd, re.I).group(1)) - 1)
                return cam and self.source.snapshot(cam["key"], None)
            q = urllib.parse.parse_qs(cmd.split("?", 1)[1])
            cam = self.cam_for(int(q["ch"][0]))
            return cam and self.source.snapshot(cam["key"], int(q["jpeg_time"][0]))
        except Exception as e:
            _LOGGER.debug("jpeg %s: %r", cmd, e)
            return None

    # ------------------------------------------------------------ video pieces
    def piece(self, key: str, start: int, length: int = CLIP) -> tuple:
        """(codec, [(t_abs, frame, is_key)]) for `length` s from `start`; cached and shared between viewers."""
        ck = (key, start, length)
        with self._clip_lock:
            v = self._clips.get(ck)
            mine = v is None
            if mine:
                v = self._clips[ck] = threading.Event()
            elif not isinstance(v, threading.Event):
                self._clips.move_to_end(ck)
                return v
        if not mine:
            v.wait(90)
            with self._clip_lock:
                r = self._clips.get(ck)
            return r if not isinstance(r, threading.Event) else (None, [])
        res: tuple = (None, [])
        try:
            data = self.source.video(key, start, start + length)
            if data:
                codec, nals = nvr_mp4.demux_h264(data)
                big = max((len(n) for _, n, p in nals if not p and (n[4] & 0x1f) == 5), default=0)
                if big > KEY_LIMIT or (codec and codec not in ("avc1", "avc3")):
                    small = self.source.downscale(data, length)      # also turns H.265 into H.264
                    if small:
                        codec, nals = nvr_mp4.demux_h264(small)
                # t=0 is the requested start (on a keyframe boundary); Protect adds a few seconds past the end.
                res = (codec, nvr_mp4.frames_of([(start + t, n, p) for t, n, p in nals if t < length - 0.001]))
        except Exception as e:
            _LOGGER.warning("video %s @%s: %r", key, start, e)
        with self._clip_lock:
            self._clips[ck] = res
            while len(self._clips) > 24:
                self._clips.popitem(last=False)
        v.set()
        return res

    def _prefetch(self, key: str, start: int) -> None:
        threading.Thread(target=self.piece, args=(key, start), daemon=True).start()

    # ------------------------------------------------------------ stream sessions
    def _stream(self, conn, peer: str, cmd: str, early: bytes) -> None:
        q = dict(kv.split("=", 1) for kv in cmd.split("&")[1:] if "=" in kv)
        try:
            fmt = "%Y/%m/%d %H:%M:%S" if q["time"].count(":") == 2 else "%Y/%m/%d %H:%M"
            t0 = dt.datetime.strptime(q["date"] + " " + q["time"], fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except (KeyError, ValueError):
            t0 = time.time() - 60
        thumb = cmd.lower().startswith("getpbthumbnail")
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nConnection: keep-alive\r\n\r\n")
        st = {"ch": int(q["ch"]) - 1 if "ch" in q else None, "speed": 1, "closed": False, "buf": b"", "ts": t0}

        def reader():
            conn.settimeout(1)
            chunk = early
            while not st["closed"]:
                st["buf"] += chunk           # first pass: commands that arrived with the request itself
                while len(st["buf"]) >= 24:
                    c, st["buf"] = st["buf"][:24], st["buf"][24:]
                    mask, = struct.unpack_from("<I", c, 0)
                    op, lvl = c[0x12], c[0x11]
                    self._t(f"{peer} cmd op=0x{op:02x} lvl={lvl} mask=0x{mask:x}")
                    if op == 0x11 and mask:
                        st["ch"], st["speed"] = (mask & -mask).bit_length() - 1, 1
                    elif op == 0x11:
                        st["speed"] = 1
                    elif op == 0x13:
                        st["speed"] = FF_SPEED.get(lvl, 16)
                    # 0x18 rewind: not supported yet, keeps playing forward
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    chunk = b""
                    continue
                except OSError:
                    break
                if not chunk:
                    self._t(f"{peer} controller closed the stream")
                    break
            st["closed"] = True

        threading.Thread(target=reader, daemon=True).start()
        for _ in range(20):     # continuous play names no channel in the URL; the select command follows
            if st["ch"] is not None:
                break
            time.sleep(0.02)
        if st["ch"] is None:
            used = self.used()
            st["ch"] = used[0][0] if used else 0
        cam = self.cam_for(st["ch"])
        _LOGGER.debug("%s %s ch=%d from %s -> %s", peer, "preview" if thumb else "play", st["ch"] + 1,
                      dt.datetime.fromtimestamp(t0, dt.timezone.utc), cam and cam["name"])
        self._t(f"{peer} {'preview' if thumb else 'play'} ch={st['ch'] + 1} at {int(t0)} -> {cam and cam['name']}")
        st["sent"] = 0
        why = "done"
        try:
            if cam:
                self._send_loop(conn, st, cam["key"], t0, thumb)
        except OSError as e:
            why = repr(e)
        finally:
            st["closed"] = True
            self._t(f"{peer} stream ended ({why}), {st['sent']} frames sent")

    def _get_piece(self, conn, st, key, start, length=CLIP):
        """Fetch a piece, sending AUD keep-alives so the controller's ~1 s read timeout doesn't fire."""
        box: dict = {}
        th = threading.Thread(target=lambda: box.setdefault("r", self.piece(key, start, length)), daemon=True)
        th.start()
        while th.is_alive() and not st["closed"]:
            th.join(0.25)
            if th.is_alive():
                conn.sendall(packet(AUD, st["ts"], st["ch"]))
        return box.get("r", (None, []))

    def _send_loop(self, conn, st, key, t0, thumb) -> None:
        piece_start = int(t0) - int(t0) % CLIP
        ch_now = st["ch"]
        if thumb:
            # Seek preview: the controller only wants the keyframe at/before t0 (and gives up after ~5 s), so fetch
            # 1 s from that keyframe and start the full piece for the play that may follow.
            self._prefetch(key, piece_start)
            _, frames = self._get_piece(conn, st, key, int(t0) - int(t0) % GOP, 1)
            if frames and not st["closed"]:
                t, frame, _ = frames[0]
                conn.sendall(packet(frame, t, ch_now))
                end = time.time() + 5
                while not st["closed"] and time.time() < end:
                    time.sleep(0.1)
            return
        first, wall0, media0 = True, None, 0.0
        while not st["closed"]:
            if st["ch"] != ch_now:
                cam = self.cam_for(st["ch"])
                if not cam:
                    return
                key, ch_now, first, wall0 = cam["key"], st["ch"], True, None
            codec, frames = self._get_piece(conn, st, key, piece_start)
            self._prefetch(key, piece_start + CLIP)
            if not frames:
                if time.time() - piece_start < CLIP + 5:      # live edge: not recorded yet
                    time.sleep(1)
                    with self._clip_lock:
                        self._clips.pop((key, piece_start, CLIP), None)
                    continue
                piece_start += CLIP                              # a gap in the recording
                continue
            if first:
                starts = [i for i, f in enumerate(frames) if f[2]]
                before = [i for i in starts if frames[i][0] <= t0]
                frames = frames[before[-1] if before else (starts[0] if starts else 0):]
                first = False
            for t, frame, is_key in frames:
                if st["closed"] or st["ch"] != ch_now:
                    break
                speed = st["speed"]
                if speed >= 4 and not is_key:
                    continue                                     # fast forward: keyframes only
                if wall0 is None:
                    wall0, media0 = time.time(), t
                due = wall0 + (t - media0) / speed
                delay = due - time.time()
                while delay > 0 and not st["closed"] and st["speed"] == speed:
                    time.sleep(min(delay, 0.2))
                    delay = due - time.time()
                if st["speed"] != speed or delay < -2:
                    wall0, media0 = time.time(), t               # speed change, or fell behind: re-anchor
                conn.sendall(packet(frame, t, ch_now))
                st["ts"] = t
                st["sent"] += 1
            piece_start += CLIP

    # ------------------------------------------------------------ virtual ONVIF cameras
    def _onvif_conn(self, conn, addr, c) -> None:
        try:
            conn.settimeout(10)
            line, _headers, body = _read_request(conn)
            if line is None:
                return
            if line.startswith("GET"):
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            m = re.search(rb"<(?:[A-Za-z0-9_]+:)?Body[^>]*>\s*<(?:[A-Za-z0-9_]+:)?([A-Za-z]+)", body, re.S)
            action = m.group(1).decode() if m else "?"
            self._t(f"{addr[0]} ONVIF[{c['name']}] {action}")
            reply = self._onvif_reply(action, c)
            out = (_envelope(reply) if reply else _fault(f"{action} not supported")).encode()
            status = b"200 OK" if reply else b"500 Internal Server Error"
            conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/soap+xml; charset=utf-8\r\n"
                         b"Content-Length: " + str(len(out)).encode() + b"\r\nConnection: close\r\n\r\n" + out)
        except Exception as e:
            _LOGGER.debug("ONVIF %s: %r", c["name"], e)
        finally:
            conn.close()

    def _onvif_reply(self, action: str, c: dict):
        xa = f"http://{self.ip}:{c['port']}/onvif"
        w, h = c["width"], c["height"]
        src = (f'<tt:VideoSourceConfiguration token="vsc0"><tt:Name>vsc0</tt:Name><tt:UseCount>1</tt:UseCount>'
               f'<tt:SourceToken>vs0</tt:SourceToken><tt:Bounds x="0" y="0" width="{w}" height="{h}"/>'
               "</tt:VideoSourceConfiguration>")

        def enc(tag="tt:VideoEncoderConfiguration", attrs=' token="vec0"'):
            return (f"<{tag}{attrs}><tt:Name>vec0</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>"
                    f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
                    "<tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>"
                    "<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit>"
                    "</tt:RateControl><tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Main</tt:H264Profile>"
                    "</tt:H264><tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0"
                    "</tt:IPv4Address></tt:Address><tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false"
                    f"</tt:AutoStart></tt:Multicast><tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>")

        def profile(tag="trt:Profiles"):
            return f'<{tag} token="main" fixed="true"><tt:Name>main</tt:Name>{src}{enc()}</{tag}>'

        def uri(tag, link):
            return (f"<trt:{tag}Response><trt:MediaUri><tt:Uri>{link}</tt:Uri><tt:InvalidAfterConnect>false"
                    "</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S"
                    f"</tt:Timeout></trt:MediaUri></trt:{tag}Response>")

        now = dt.datetime.now(dt.timezone.utc)
        replies = {
            "GetSystemDateAndTime": lambda: (
                "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
                "<tt:DaylightSavings>false</tt:DaylightSavings><tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
                f"<tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
                f"<tt:Second>{now.second}</tt:Second></tt:Time><tt:Date><tt:Year>{now.year}</tt:Year>"
                f"<tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"),
            "GetDeviceInformation": lambda: (
                "<tds:GetDeviceInformationResponse><tds:Manufacturer>UniFi Protect via HA</tds:Manufacturer>"
                f"<tds:Model>{_xml(c['name'])}</tds:Model><tds:FirmwareVersion>1.0</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{c['uuid'][:8]}</tds:SerialNumber><tds:HardwareId>HA-ONVIF</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>"),
            "GetCapabilities": lambda: (
                f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Device><tt:XAddr>{xa}/device_service</tt:XAddr>"
                f"</tt:Device><tt:Media><tt:XAddr>{xa}/media_service</tt:XAddr><tt:StreamingCapabilities>"
                "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true"
                "</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media></tds:Capabilities>"
                "</tds:GetCapabilitiesResponse>"),
            "GetServices": lambda: "<tds:GetServicesResponse>" + "".join(
                f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/{ns}/wsdl</tds:Namespace><tds:XAddr>{xa}/"
                f"{ns}_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>40</tt:Minor></tds:Version>"
                "</tds:Service>" for ns in ("device", "media")) + "</tds:GetServicesResponse>",
            "GetScopes": lambda: (
                "<tds:GetScopesResponse><tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>"
                f"onvif://www.onvif.org/name/{_xml(c['name']).replace(' ', '_')}</tt:ScopeItem></tds:Scopes>"
                "</tds:GetScopesResponse>"),
            "GetProfiles": lambda: f"<trt:GetProfilesResponse>{profile()}</trt:GetProfilesResponse>",
            "GetProfile": lambda: f"<trt:GetProfileResponse>{profile('trt:Profile')}</trt:GetProfileResponse>",
            "GetVideoSources": lambda: (
                '<trt:GetVideoSourcesResponse><trt:VideoSources token="vs0"><tt:Framerate>15</tt:Framerate>'
                f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
                "</trt:VideoSources></trt:GetVideoSourcesResponse>"),
            "GetVideoSourceConfigurations": lambda: (
                "<trt:GetVideoSourceConfigurationsResponse>"
                + src.replace("tt:VideoSourceConfiguration ", "trt:Configurations ").replace(
                    "</tt:VideoSourceConfiguration>", "</trt:Configurations>")
                + "</trt:GetVideoSourceConfigurationsResponse>"),
            "GetVideoEncoderConfigurations": lambda: (
                f"<trt:GetVideoEncoderConfigurationsResponse>{enc('trt:Configurations')}"
                "</trt:GetVideoEncoderConfigurationsResponse>"),
            "GetVideoEncoderConfiguration": lambda: (
                f"<trt:GetVideoEncoderConfigurationResponse>{enc('trt:Configuration')}"
                "</trt:GetVideoEncoderConfigurationResponse>"),
            "GetVideoEncoderConfigurationOptions": lambda: (
                "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options><tt:QualityRange><tt:Min>1</tt:Min>"
                "<tt:Max>10</tt:Max></tt:QualityRange><tt:H264><tt:ResolutionsAvailable>"
                f"<tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:ResolutionsAvailable>"
                "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>300</tt:Max></tt:GovLengthRange>"
                "<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>"
                "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
                "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264></trt:Options>"
                "</trt:GetVideoEncoderConfigurationOptionsResponse>"),
            "GetStreamUri": lambda: uri("GetStreamUri", _xml(c["rtsp"])),
            "GetSnapshotUri": lambda: uri("GetSnapshotUri", f"http://{self.ip}:{c['port']}/snapshot.jpg"),
            "SetVideoEncoderConfiguration": lambda: "<trt:SetVideoEncoderConfigurationResponse/>",
            "SetSystemDateAndTime": lambda: "<tds:SetSystemDateAndTimeResponse/>",
            "SetNTP": lambda: "<tds:SetNTPResponse/>",
        }
        fn = replies.get(action)
        return fn() if fn else None

    def _probe_match(self, c: dict, relates_to: str) -> str:
        name = _xml(c["name"]).replace(" ", "_")
        scopes = (f"onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/Profile/Streaming "
                  f"onvif://www.onvif.org/name/{name} onvif://www.onvif.org/hardware/HA-ONVIF")
        header = (f"<wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID><wsa:RelatesTo>{relates_to}</wsa:RelatesTo>"
                  "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
                  "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>")
        body = ("<d:ProbeMatches><d:ProbeMatch><wsa:EndpointReference><wsa:Address>urn:uuid:"
                f"{c['uuid']}</wsa:Address></wsa:EndpointReference><d:Types>dn:NetworkVideoTransmitter</d:Types>"
                f"<d:Scopes>{scopes}</d:Scopes><d:XAddrs>http://{self.ip}:{c['port']}/onvif/device_service</d:XAddrs>"
                "<d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches>")
        return _envelope(body, header)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _envelope(body: str, header: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<s:Envelope xmlns:s="{SOAP_ENV}" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>")


def _fault(reason: str) -> str:
    return _envelope('<s:Fault><s:Code><s:Value>s:Receiver</s:Value><s:Subcode><s:Value>ter:ActionNotSupported'
                     f'</s:Value></s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason>'
                     "</s:Fault>")


def _read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk or len(data) > 1 << 20:
            return None, None, b""
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
    n = int(headers.get("content-length", 0) or 0)
    while len(rest) < n:
        chunk = conn.recv(65536)
        if not chunk:
            break
        rest += chunk
    return lines[0], headers, rest
