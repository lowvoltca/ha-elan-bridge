"""elan_bridge protocol, id numbering and client, against a simulated HA Bridge driver.

    python3 -m unittest tests.test_bridge -v
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "custom_components", "elan_bridge")

pkg = types.ModuleType("elan_bridge")
pkg.__path__ = [PKG_DIR]
sys.modules["elan_bridge"] = pkg


def load(name):
    spec = importlib.util.spec_from_file_location("elan_bridge." + name, os.path.join(PKG_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["elan_bridge." + name] = mod
    spec.loader.exec_module(mod)
    return mod


p = load("protocol")
const = load("const")
client_mod = load("client")
ml = load("media_logic")
ev = load("events_logic")
sip = load("sip_door")


class StationIdTest(unittest.TestCase):
    def test_new_station_ids_are_local_and_unique(self):
        ids = {const.new_station_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for sid in ids:
            self.assertRegex(sid, r"^02[0-9A-F]{10}$")      # locally administered, never ELAN's F8572E prefix

class SipAudioTest(unittest.TestCase):
    def test_rtp_payload(self):
        import struct
        plain = struct.pack("!BBHII", 0x80, 0, 1, 160, 7) + b"\x11" * 160
        self.assertEqual(sip.rtp_payload(plain), (0, b"\x11" * 160))
        csrc_ext = (struct.pack("!BBHII", 0x91, 0x80 | 0, 1, 160, 7) + b"CSRC"
                    + struct.pack("!HH", 0xBEDE, 1) + b"EXT!" + b"\x22" * 160)
        self.assertEqual(sip.rtp_payload(csrc_ext), (0, b"\x22" * 160))
        padded = struct.pack("!BBHII", 0xA0, 101, 1, 160, 7) + b"\x33" * 4 + b"\x00\x00\x03"
        self.assertEqual(sip.rtp_payload(padded), (101, b"\x33" * 4))
        self.assertEqual(sip.rtp_payload(b"junk"), (None, b""))

    def test_elan_audio_reaches_callback(self):
        import struct
        audio = sip.RtpAudio(("127.0.0.1", 9))
        got = []
        audio.on_audio = got.append
        audio.datagram_received(struct.pack("!BBHII", 0x80, 0, 1, 160, 7) + b"\x44" * 160, ("10.0.0.1", 8094))
        audio.datagram_received(struct.pack("!BBHII", 0x80, 101, 2, 160, 7) + b"\x01\x0a\x00\xa0", ("10.0.0.1", 8094))
        self.assertEqual(got, [b"\x44" * 160])          # DTMF isn't sound
        self.assertEqual(audio.remote, ("10.0.0.1", 8094))   # replies follow ELAN's source
        self.assertEqual(audio.received, 2)

    def test_talkback_command(self):
        cmd = sip.talkback_command("ffmpeg", {"host": "10.0.0.5", "port": 7004, "rate": 22050, "channels": 1,
                                              "bitrate": 16000})
        self.assertEqual(cmd[cmd.index("-i") + 1], "pipe:0")
        self.assertEqual(cmd[cmd.index("-f") + 1], "mulaw")
        self.assertIn("adts", cmd)
        self.assertEqual(cmd[-1], "udp://10.0.0.5:7004?bitrate=24000")
        self.assertNotIn("-nostdin", cmd)               # it reads the call's audio from stdin

    def test_talkback_command_opus(self):
        cmd = sip.talkback_command("ffmpeg", {"host": "10.0.0.5", "port": 7004, "codec": "opus", "rate": 22050,
                                              "channels": 1, "bitrate": 16000})
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "libopus")
        self.assertEqual(cmd[cmd.index("-ar", cmd.index("-c:a")) + 1], "48000")   # 22050 isn't an Opus rate
        self.assertEqual(cmd[-2:], ["rtp", "rtp://10.0.0.5:7004"])

class EventsTest(unittest.TestCase):
    def test_classify_unifi_protect_names(self):
        c = ev.classify
        self.assertEqual(c("binary_sensor", [None, None], "binary_sensor.front_yard_g5_person_detected"),
                         ("binary", "Person Detected"))
        self.assertEqual(c("binary_sensor", ["Car alarm detected"], "binary_sensor.x"), ("binary", ev.AUDIO))
        self.assertEqual(c("binary_sensor", [None], "binary_sensor.g4_doorbell_pro_poe_doorbell"),
                         ("binary", "Doorbell Detected"))
        self.assertEqual(c("binary_sensor", [None], "binary_sensor.g4_doorbell_pro_poe_is_dark"), ("binary", "Is Dark"))
        self.assertEqual(c("binary_sensor", [None], "binary_sensor.g4_doorbell_pro_poe_motion"),
                         ("binary", "Motion Detected"))
        self.assertEqual(c("event", [None], "event.g4_doorbell_pro_poe_doorbell"),
                         ("event", "Doorbell Detected", "Last Doorbell Ring"))
        self.assertEqual(c("event", ["NFC"], "event.x"), ("event", None, "Last NFC Event"))
        self.assertIsNone(c("event", [None], "event.cameras_g4_doorbell_pro_poe_smart_detection"))
        self.assertIsNone(c("sensor", ["Person detected"], "sensor.x"))

    def test_combined_state(self):
        s = ev.EventStates()
        self.assertTrue(s.set(ev.AUDIO, "siren", True))        # first on: event goes On
        self.assertIsNone(s.set(ev.AUDIO, "glass", True))      # already on
        self.assertIsNone(s.set(ev.AUDIO, "siren", False))     # glass still on
        self.assertFalse(s.set(ev.AUDIO, "glass", False))      # last off: event goes Off

    def test_lines_and_ports(self):
        self.assertEqual(p.camera_event("Person Detected", True), "EVENT Person Detected On")
        self.assertEqual(p.named_var("Last Doorbell Ring", "2026-11-14 12:00:00"),
                         "VAR Last Doorbell Ring=2026-11-14 12:00:00")
        self.assertEqual(ev.assign_ports(["dev1", "dev2"], {}), {"dev1": 10031, "dev2": 10032})


class MediaTest(unittest.TestCase):
    def test_parse_buttons(self):
        self.assertEqual(p.parse("FUNC Play/Pause"), p.Command("FUNC", value="Play/Pause"))
        self.assertEqual(p.parse("STOP Volume Up\r"), p.Command("STOP", value="Volume Up"))
        self.assertEqual(p.parse("ACTIVE true"), p.Command("ACTIVE", value="true"))
        self.assertIsNone(p.parse("FUNC"))
        self.assertEqual(p.var("currentTitle", "A\nB"), "VAR currentTitle A B")

    def test_ports_kept(self):
        ports = ml.assign_ports(["media_player.tv", "media_player.kitchen"], {})
        self.assertEqual(ports, {"media_player.tv": 10021, "media_player.kitchen": 10022})
        self.assertEqual(ml.assign_ports(["media_player.den"], ports)["media_player.den"], 10023)

    def test_page_values(self):
        attrs = {"friendly_name": "Living Room TV", "media_title": "Song", "media_artist": "Band",
                 "app_name": "Spotify", "entity_picture": "/api/media_player_proxy/media_player.tv?token=x",
                 "media_duration": 245, "media_position": 62}
        v = ml.page_values("playing", attrs, "http://homeassistant.local:8123")
        self.assertEqual(v["currentTitle"], "Band - Song")
        self.assertEqual(v["currentSource"], "Spotify")
        self.assertEqual(v["currentCover"], "http://homeassistant.local:8123/api/media_player_proxy/media_player.tv?token=x")
        self.assertEqual(v["currentPosition"], "1:02 / 4:05")
        self.assertEqual(ml.page_values("off", {"friendly_name": "TV"})["currentTitle"], "TV: off")
        self.assertEqual(ml.page_values("playing", {"entity_picture": "/x"}, "")["currentCover"], "")

    def test_only_changes_are_sent(self):
        sent = {}
        first = ml.page_lines({"currentTitle": "A"}, ml.button_states("playing"), sent)
        self.assertIn("VAR currentTitle A", first)
        self.assertIn("STATE Play CHECK", first)
        self.assertEqual(ml.page_lines({"currentTitle": "A"}, ml.button_states("playing"), sent), [])
        self.assertEqual(ml.page_lines({"currentTitle": "A"}, ml.button_states("paused"), sent),
                         ["STATE Play UNCHECK", "STATE Pause CHECK", "STATE Play/Pause UNCHECK"])

    def test_actions(self):
        self.assertEqual(ml.action("Play/Pause", {}, None, None), ("media_player", "media_play_pause", {}, "media"))
        self.assertEqual(ml.action("Mute Toggle", {"is_volume_muted": True}, None, None),
                         ("media_player", "volume_mute", {"is_volume_muted": False}, "media"))
        self.assertEqual(ml.action("Up", {}, "remote.tv", "apple_tv"), ("remote", "send_command", {"command": "up"}, "remote"))
        self.assertEqual(ml.action("Enter", {}, "remote.tv", "androidtv_remote")[2], {"command": "DPAD_CENTER"})
        self.assertIsNone(ml.action("Up", {}, None, None))          # no remote on the device
        self.assertIsNone(ml.action("Custom1", {}, "remote.tv", None))  # event only


class ProtocolTest(unittest.TestCase):
    def test_outgoing(self):
        self.assertEqual(p.add(101, p.DIMMER, "  Kitchen\nPendants "), "ADD 101 DIMMER Kitchen Pendants")
        self.assertEqual(p.add(102, p.SWITCH, ""), "ADD 102 SWITCH Device 102")
        self.assertEqual(p.state(101, True), "STATE 101 ON")
        self.assertEqual(p.level(101, 140), "LEVEL 101 100")

    def test_parse(self):
        self.assertEqual(p.parse("ON 101"), p.Command("ON", 101))
        self.assertEqual(p.parse("DIM 101 42.6\r"), p.Command("DIM", 101, "42"))
        self.assertEqual(p.parse("RAMP 7 UP"), p.Command("RAMP", 7, "UP"))
        self.assertEqual(p.parse("ERR bad ADD: x").value, "bad ADD: x")
        for junk in ("", "ON", "ON x", "DIM 1", "WHAT 1"):
            self.assertIsNone(p.parse(junk), junk)

    def test_lines(self):
        buf = p.LineBuffer()
        self.assertEqual(buf.feed(b"HELLO 1\r\nON 1"), ["HELLO 1"])
        self.assertEqual(buf.feed(b"01\n\nOFF 101\n"), ["ON 101", "OFF 101"])


class IdsTest(unittest.TestCase):
    def test_numbers_are_kept_and_never_reused(self):
        ids = const.assign_ids(["light.a", "switch.b"], {})
        self.assertEqual(ids, {"light.a": 101, "switch.b": 102})
        ids = const.assign_ids(["switch.b", "fan.c"], ids)    # light.a removed, fan.c added
        self.assertEqual(ids, {"light.a": 101, "switch.b": 102, "fan.c": 103})


class FakeDriver:
    """Behaves like ha_bridge/elan_driver.lua on the wire."""

    def __init__(self):
        self.received = []
        self.writers = []

    async def handle(self, reader, writer):
        self.writers.append(writer)
        while True:
            line = await reader.readline()
            if not line:
                return
            line = line.decode().strip()
            self.received.append(line)
            if line.startswith("HELLO"):
                writer.write(b"HELLO 1\n")
            elif line == "PING":
                writer.write(b"PONG\n")
            await writer.drain()

    def push(self, line):
        self.writers[-1].write((line + "\n").encode())


class ClientTest(unittest.TestCase):
    def test_connect_send_receive_reconnect(self):
        async def run():
            driver = FakeDriver()
            server = await asyncio.start_server(driver.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            commands, connects = [], []

            client = None

            async def on_connect():
                connects.append(1)
                await client.send(p.hello())
                await client.send(p.add(101, p.SWITCH, "Lamp"))

            client_mod.RECONNECT_SECONDS = (0.1,)
            client = client_mod.BridgeClient("127.0.0.1", port, commands.append, on_connect)
            await client.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if len(driver.received) >= 2:
                    break
            self.assertEqual(driver.received[:2], ["HELLO 1", "ADD 101 SWITCH Lamp"])
            driver.push("ON 101")
            driver.push("PONG")
            driver.push("DIM 101 30")
            await asyncio.sleep(0.1)
            self.assertEqual(commands, [p.Command("ON", 101), p.Command("DIM", 101, "30")])

            driver.writers[-1].close()            # ELAN restarts: the client must come back and resend
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(connects) == 2 and len(driver.received) >= 4:
                    break
            self.assertEqual(len(connects), 2)
            self.assertEqual(driver.received[2:4], ["HELLO 1", "ADD 101 SWITCH Lamp"])
            await client.close()
            server.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
