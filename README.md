# ELAN Bridge for Home Assistant

Connects Home Assistant to an ELAN control system (Nice/ELAN, firmware 9.x), in both directions:

| In ELAN you get | From Home Assistant |
|---|---|
| Lighting devices (dimmers and switches) that work both ways | Lights, switches, fans, input booleans |
| Media sources with Now Playing, artwork and transport buttons | Any media player |
| Events in Event Mapper (Person, Vehicle, Doorbell, Motion, ...) | Camera detections |
| Security zones, plus arming and disarming from ELAN | Door, window and motion sensors; an alarm panel (e.g. Alarmo) |
| An EL-NVR with recordings, timeline and live view in the ELAN Viewer | UniFi Protect cameras |
| A video intercom call on the touchpanels when someone rings | A UniFi Protect doorbell |

Everything stays on your network. Home Assistant talks to drivers on the ELAN controller over TCP, serves an
emulated EL-NVR and ONVIF cameras for video, and calls ELAN's own SIP server for the doorbell.

> **Unofficial.** Not made, supported or endorsed by Nice/ELAN or Ubiquiti. Use at your own risk.

## Requirements

- Home Assistant 2025.8 or newer.
- An ELAN controller on firmware 9.x, and access to ELAN Configurator.
- **The ELAN-side drivers** (HA Bridge, HA Media Player, HA Camera Events, HA Security System). Lights, media,
  camera events and security need these installed on the controller. They are **not** in this repository; see
  [ELAN drivers](#elan-drivers).
- For NVR playback and the doorbell: Home Assistant's UniFi Protect integration, with RTSP turned on for each
  camera in Protect.

The NVR and the doorbell use ELAN's built-in EL-NVR and SIP Server drivers, so they need no extra ELAN drivers.

## Install

With [HACS](https://hacs.xyz):

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/lowvoltca/ha-elan-bridge`, type
   **Integration**.
2. Search for **ELAN Bridge** in HACS, download it, and restart Home Assistant.
3. **Settings → Devices & services → Add integration → ELAN Bridge**. Enter the controller's IP address and the
   HA Bridge port (10010).

When a new version is released, HACS shows it under **Settings → Updates**.

Manual install: copy `custom_components/elan_bridge` into your `config/custom_components/` folder and restart.

## Setup in ELAN Configurator

Open the integration's **Configure** dialog to choose what ELAN gets. It lists the port each ELAN driver needs.

**Lights and switches.** Install the HA Bridge driver (port 10010). The entities you pick appear as ELAN lighting
devices the next time Home Assistant connects. Removing one stops its updates, but its ELAN device stays until
you delete it in Configurator.

**Media players.** Add one *HA Media Player* source per player (Media → Sources). Set its port to the one shown
in the dialog and click Apply.

**Camera events.** Add one *HA Camera Events* driver per camera, with the port from the dialog. Its detections
appear in Event Mapper.

**Security.** Install the *HA Security System* driver (Security → Security Panels), port 10040, then Apply. The
sensors you pick become zones; the dialog lists their zone numbers. If you pick an alarm panel, ELAN arms it
(Away = arm away, Stay = arm home) and disarms it.

**NVR playback (UniFi Protect).** The cameras you pick are served to ELAN as an *ELAN EL-NVR* at Home Assistant's
IP address.
1. In Video → NVR/DVR Systems, add an EL-NVR at Home Assistant's IP address.
2. Add each camera as an **ONVIF Video Source (Static)**, at Home Assistant's address with the ONVIF port from the
   dialog. Give it the same name as in Protect. The *dynamic* ONVIF driver doesn't work here: it rewrites the
   stream address and every camera reports an authorization error.
3. Pick each camera on an EL-NVR channel.

A second page in the dialog sets which Protect stream each live view uses. ELAN plays about 720p or smaller live.

**Doorbell (UniFi Protect).** Pick the doorbell in the dialog. Home Assistant calls ELAN's touchpanels as a SIP
door station when it's pressed, and sends its video and audio when a panel answers. You only do this once:
1. In Messaging → Communication Devices → **ELAN SIP Server**, set Enrollment Mode to **Open**.
2. Save the dialog in Home Assistant and wait for a **SIP Station** to appear in Configurator. Rename it if you
   like; the touchpanels show that name.
3. Set Enrollment Mode back to **Closed**.

To test without pressing the doorbell, call the action `elan_bridge.ring_doorbell`.

## ELAN drivers

The ELAN-side drivers are ELAN driver packages (`.EDRVC`) that must be installed on the controller through
Configurator. They aren't published here yet, because they need to be packaged and signed in a way ELAN
supports. Until then, open an issue if you'd like to help test them.

## Troubleshooting

- **"The HA Bridge driver didn't answer"**: check that the driver shows *Listening on port 10010* in
  Configurator. The driver opens its port about a minute after ELAN starts.
- **Cameras show authorization errors in ELAN**: they were added with the dynamic ONVIF driver. Re-add them as
  **ONVIF Video Source (Static)**.
- **No video in the NVR or the doorbell call**: turn on RTSP for that camera in UniFi Protect.
- **Doorbell never appears in Configurator**: Enrollment Mode must be Open while Home Assistant registers.
  Home Assistant needs UDP 5062 free, and the controller must be able to reach it.
- To see what's happening, add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.elan_bridge: debug
  ```

## Ports used

| Where | Port | What |
|---|---|---|
| ELAN controller | TCP 10010 | HA Bridge (lighting) |
| ELAN controller | TCP 10021+ | HA Media Player, one per player |
| ELAN controller | TCP 10031+ | HA Camera Events, one per camera |
| ELAN controller | TCP 10040 | HA Security System |
| ELAN controller | UDP 5060 | ELAN SIP Server |
| Home Assistant | TCP 80 | EL-NVR emulator |
| Home Assistant | TCP 8001+ | ONVIF camera, one per camera |
| Home Assistant | UDP 5062, 40000, 40002 | Doorbell SIP and media |
| Home Assistant | UDP 1900, 3702 | Discovery replies (SSDP, WS-Discovery) |

The NVR emulator needs port 80 on Home Assistant's address to be free.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Releasing: bump `version` in `custom_components/elan_bridge/manifest.json`, commit, then publish a GitHub release
tagged `v<version>` (e.g. `v0.7.0`) with notes on what changed. The release check fails if the tag and the
manifest differ.

## License

MIT, see [LICENSE](LICENSE).
