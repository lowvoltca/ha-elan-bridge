"""HA doorbell -> ELAN intercom: Home Assistant calls ELAN's SIP Server as a door station.

ELAN side (firmware 9.0.59 libLSIP.so):
  * Messaging -> Communication Devices -> ELAN SIP Server listens on UDP 5060 of the controller.
  * A station is known by its Contact up to the '@' ("sip:<id>"). An unknown one that REGISTERs while the SIP
    Server's Enrollment Mode is Open is installed as a "SIP Station"; ELAN answers 200 OK ";expires=60", so we
    re-register every 30 s.
  * INVITE from a registered station: 100 Trying, 180 Ringing, the touchpanels ring. Answering on a panel gives
    200 OK with ELAN's SDP: PCMU audio (sendrecv) and H264 video (recvonly). Hanging up there sends BYE.

This module has no Home Assistant imports, so it can be run on its own for tests.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

_LOGGER = logging.getLogger(__name__)

SIP_PORT = 5062            # ours; ELAN's SIP Server is on 5060
RTP_AUDIO = 40000
RTP_VIDEO = 40002
REGISTER_EVERY = 30        # ELAN grants 60 s
RING_TIMEOUT = 30          # unanswered: CANCEL after this many seconds
MAX_CALL = 180             # answered: hang up after this many seconds
USER_AGENT = "HA-Door/1"
LEGACY_STATION_ID = "02DB0E11A001"    # used by installs made before 0.7.0, which had no per-install id


def _tag() -> str:
    return "%08x" % random.getrandbits(32)


def header(msg: str, name: str) -> str:
    for line in msg.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def sdp_media(sdp: str) -> dict:
    """{"audio": (ip, port), "video": (ip, port)} from an SDP body; missing media are left out."""
    ip, out = None, {}
    for line in sdp.splitlines():
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m="):
            kind, port = line[2:].split()[:2]
            if int(port) > 0 and ip:
                out[kind] = (ip, int(port))
    return out


def media_command(ffmpeg: str, source: str, audio: tuple | None, video: tuple | None) -> list:
    """ffmpeg arguments that send the doorbell's H.264 (copied, SPS/PPS repeated in-band) and its audio as PCMU."""
    cmd = [ffmpeg, "-nostdin", "-loglevel", "warning"]
    if source.startswith("rtsp"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", source]
    if video:
        cmd += ["-map", "0:v:0", "-c:v", "copy", "-bsf:v", "dump_extra", "-f", "rtp", "-payload_type", "96",
                f"rtp://{video[0]}:{video[1]}?localrtpport={RTP_VIDEO}&pkt_size=1200"]
    if audio:
        cmd += ["-map", "0:a:0?", "-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1", "-f", "rtp", "-payload_type", "0",
                f"rtp://{audio[0]}:{audio[1]}?localrtpport={RTP_AUDIO}"]
    return cmd


class DoorStation(asyncio.DatagramProtocol):
    """One SIP door station registered with one ELAN controller. ring() calls the touchpanels."""

    def __init__(self, controller: str, local_ip: str, station_id: str, name: str, media_source=None,
                 ffmpeg: str | None = None) -> None:
        self.ctrl = (controller, 5060)
        self.local = local_ip
        self.station_id = station_id
        self.name = name
        self.media_source = media_source     # async callable -> stream url (or None for no media)
        self.ffmpeg = ffmpeg
        self.transport = None
        self.registered = False
        self._cseq = 0
        self._reg_call_id = f"{_tag()}{_tag()}@{local_ip}"
        self._reg_tag = _tag()
        self._tasks: set = set()
        self._call = None                    # dict while a call is in progress
        self._proc = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", SIP_PORT))
        self._spawn(self._register_loop())

    def close(self) -> None:
        """Synchronous, so the port is free before a reload starts the next station."""
        call = self._call
        if call and call["answered"]:
            self._send(f"BYE sip:{self.ctrl[0]} SIP/2.0\n"
                       + self._headers("BYE", call["id"], call["tag"], self._next_cseq(), call["to_tag"],
                                       extra="Content-Length: 0\n") + "\n")
        self._call = None
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        self._proc = None
        for task in list(self._tasks):
            task.cancel()
        if self.transport:
            self.transport.close()
            self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ wire
    def _send(self, text: str) -> None:
        if self.transport:
            self.transport.sendto(text.replace("\r\n", "\n").replace("\n", "\r\n").encode(), self.ctrl)
            _LOGGER.debug("SIP >>> %s", text.splitlines()[0])

    def _headers(self, method: str, call_id: str, from_tag: str, cseq: int, to_tag: str | None = None,
                 branch: str | None = None, extra: str = "") -> str:
        to = f"<sip:{self.ctrl[0]}>" + (f";tag={to_tag}" if to_tag else "")
        return (f"Via: SIP/2.0/UDP {self.local}:{SIP_PORT};branch={branch or 'z9hG4bK' + _tag()};rport\n"
                f"Max-Forwards: 70\n"
                f"From: \"{self.name}\" <sip:{self.station_id}@{self.local}>;tag={from_tag}\n"
                f"To: {to}\nCall-ID: {call_id}\nCSeq: {cseq} {method}\n"
                f"Contact: <sip:{self.station_id}@{self.local}:{SIP_PORT}>\nUser-Agent: {USER_AGENT}\n{extra}")

    def _next_cseq(self) -> int:
        self._cseq += 1
        return self._cseq

    def _reply(self, request: str, status: str) -> None:
        keep = ("via", "from", "to", "call-id", "cseq")
        lines = [ln for ln in request.splitlines() if ln.split(":", 1)[0].strip().lower() in keep]
        self._send(f"SIP/2.0 {status}\n" + "\n".join(lines) + "\nContent-Length: 0\n\n")

    # ------------------------------------------------------------------ registration
    async def _register_loop(self) -> None:
        while True:
            self._send(f"REGISTER sip:{self.ctrl[0]} SIP/2.0\n"
                       + self._headers("REGISTER", self._reg_call_id, self._reg_tag, self._next_cseq(),
                                       extra="Expires: 60\nContent-Length: 0\n") + "\n")
            await asyncio.sleep(REGISTER_EVERY)
            if not self.registered:
                _LOGGER.warning("ELAN doorbell: the SIP Server at %s doesn't accept station %s. Set its Enrollment "
                                "Mode to Open in Configurator (Messaging -> Communication Devices -> ELAN SIP "
                                "Server) until a SIP Station appears", self.ctrl[0], self.station_id)

    # ------------------------------------------------------------------ calls
    async def ring(self) -> bool:
        """Call ELAN. False when a call is already going (or ELAN hasn't accepted the station)."""
        if self._call:
            _LOGGER.debug("ELAN doorbell: already calling")
            return False
        if not self.registered:
            _LOGGER.warning("ELAN doorbell: ringing although ELAN hasn't accepted the station yet")
        call = {"id": f"{_tag()}{_tag()}@{self.local}", "tag": _tag(), "cseq": self._next_cseq(),
                "branch": "z9hG4bK" + _tag(), "to_tag": None, "answered": False, "start": time.monotonic()}
        self._call = call
        sdp = (f"v=0\no=- {random.randint(1, 1 << 30)} 1 IN IP4 {self.local}\ns={self.name}\n"
               f"c=IN IP4 {self.local}\nt=0 0\n"
               f"m=audio {RTP_AUDIO} RTP/AVP 0 101\na=rtpmap:0 PCMU/8000\n"
               f"a=rtpmap:101 telephone-event/8000\na=fmtp:101 0-15\na=sendrecv\n"
               f"m=video {RTP_VIDEO} RTP/AVP 96\na=rtpmap:96 H264/90000\n"
               f"a=fmtp:96 profile-level-id=42e01f;packetization-mode=1\na=sendonly\n").replace("\n", "\r\n")
        call["invite"] = (f"INVITE sip:{self.ctrl[0]} SIP/2.0\n"
                          + self._headers("INVITE", call["id"], call["tag"], call["cseq"], branch=call["branch"],
                                          extra=f"Content-Type: application/sdp\nContent-Length: {len(sdp)}\n")
                          + "\n")
        self._send(call["invite"] + sdp)
        _LOGGER.info("ELAN doorbell: calling ELAN")
        self._spawn(self._watch(call))
        return True

    async def _watch(self, call: dict) -> None:
        while self._call is call:
            await asyncio.sleep(1)
            age = time.monotonic() - call["start"]
            if not call["answered"] and age > RING_TIMEOUT:
                self._send(f"CANCEL sip:{self.ctrl[0]} SIP/2.0\n"
                           + self._headers("CANCEL", call["id"], call["tag"], call["cseq"], branch=call["branch"],
                                           extra="Content-Length: 0\n") + "\n")
                _LOGGER.info("ELAN doorbell: not answered")
                await self._end_call()
            elif call["answered"] and age > MAX_CALL:
                await self._hang_up("call time limit")

    async def _hang_up(self, why: str) -> None:
        call = self._call
        if call and call["answered"]:
            self._send(f"BYE sip:{self.ctrl[0]} SIP/2.0\n"
                       + self._headers("BYE", call["id"], call["tag"], self._next_cseq(), call["to_tag"],
                                       extra="Content-Length: 0\n") + "\n")
        _LOGGER.info("ELAN doorbell: hung up (%s)", why)
        await self._end_call()

    async def _end_call(self) -> None:
        self._call = None
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()

    async def _answered(self, call: dict, response: str) -> None:
        call["answered"] = True
        call["to_tag"] = header(response, "To").split("tag=")[-1].split(";")[0] or None
        self._send(f"ACK sip:{self.ctrl[0]} SIP/2.0\n"
                   + self._headers("ACK", call["id"], call["tag"], call["cseq"], call["to_tag"],
                                   extra="Content-Length: 0\n") + "\n")
        _LOGGER.info("ELAN doorbell: answered")
        media = sdp_media(response.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in response else response)
        source = await self.media_source() if self.media_source else None
        if not (source and self.ffmpeg and media):
            _LOGGER.warning("ELAN doorbell: answered, but no doorbell stream to send")
            return
        cmd = media_command(self.ffmpeg, source, media.get("audio"), media.get("video"))
        _LOGGER.debug("ELAN doorbell media: %s", " ".join(cmd).split(" -i ")[0])
        self._proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.PIPE)
        self._spawn(self._log_ffmpeg(self._proc))

    async def _log_ffmpeg(self, proc) -> None:
        async for line in proc.stderr:
            _LOGGER.debug("ffmpeg: %s", line.decode(errors="replace").rstrip())
        await proc.wait()
        if proc.returncode not in (0, -15, 255) and self._proc is proc:
            _LOGGER.warning("ELAN doorbell: the stream to ELAN stopped (ffmpeg exit %s)", proc.returncode)

    # ------------------------------------------------------------------ incoming
    def datagram_received(self, data: bytes, addr) -> None:
        msg = data.decode("utf-8", "replace")
        first = msg.split("\r\n", 1)[0]
        _LOGGER.debug("SIP <<< %s", first)
        if first.startswith("SIP/2.0 "):
            self._response(first, msg)
            return
        method = first.split(" ", 1)[0]
        call = self._call
        if method == "BYE":
            self._reply(msg, "200 OK")
            if call and header(msg, "Call-ID") == call["id"]:
                _LOGGER.info("ELAN doorbell: ELAN hung up")
                self._spawn(self._end_call())
        elif method == "INVITE":
            self._reply(msg, "486 Busy Here")       # a panel calling the door: not supported yet
        elif method == "ACK":
            pass
        else:                                        # OPTIONS, NOTIFY, INFO (door strike / DTMF), MESSAGE
            if method == "INFO":
                _LOGGER.info("ELAN doorbell: ELAN sent INFO %s", msg.split("\r\n\r\n", 1)[-1].strip()[:80])
            self._reply(msg, "200 OK")

    def _response(self, first: str, msg: str) -> None:
        code = int(first.split()[1])
        cseq = header(msg, "CSeq")
        if cseq.endswith("REGISTER"):
            ok = code == 200
            if ok != self.registered:
                _LOGGER.info("ELAN doorbell: station %s %s", self.station_id,
                             "registered with ELAN" if ok else f"refused ({first})")
            self.registered = ok
            return
        call = self._call
        if not call or header(msg, "Call-ID") != call["id"] or not cseq.endswith("INVITE"):
            return
        if code == 200 and not call["answered"]:
            self._spawn(self._answered(call, msg))
        elif code >= 300:
            _LOGGER.info("ELAN doorbell: ELAN ended the call (%s)", first[8:])
            to_hdr = header(msg, "To")
            self._send(f"ACK sip:{self.ctrl[0]} SIP/2.0\n"       # every final non-2xx answer is ACKed
                       + self._headers("ACK", call["id"], call["tag"], call["cseq"],
                                       to_hdr.split("tag=")[-1].split(";")[0] if "tag=" in to_hdr else None,
                                       branch=call["branch"], extra="Content-Length: 0\n") + "\n")
            self._spawn(self._end_call())
