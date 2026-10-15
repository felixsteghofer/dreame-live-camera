# Dreame Live Camera

Experimental, Python-only Home Assistant custom integration for the physical
camera in compatible Dreame/MOVA vacuums. Uses the already configured
`dreame_vacuum` integration's authenticated cloud transport. No Node.js server,
external bridge, cloud password copy or robot firmware change is required.

## Install and configure

Source and issue tracker: [felixsteghofer/dreame-live-camera](https://github.com/felixsteghofer/dreame-live-camera).

Copy `custom_components/dreame_live_camera` from this repository into Home Assistant's
`custom_components` directory and restart Core. Add **Dreame Live Camera** under
Settings → Devices & services. Select the existing vacuum and enter its camera
PIN. PIN and session-duration settings can be changed through integration options.
The PIN is stored in HA configuration, never in entity attributes or logs.

The integration creates a live camera plus Start and Stop button entities. Start
with the button or `camera.turn_on`; then open the camera or use a standard
picture-entity card with `camera_view: live`. `camera.turn_off` and the Stop button
end the monitor. Close live view in the manufacturer's app before starting.

A preview/snapshot never starts the camera. An active session ends after its
configured limit (default 120 seconds), after 30 seconds with no initial viewer,
or two seconds after the last live-stream viewer disconnects. Some clients retain
the MJPEG connection after closing a popup; use Stop to end the camera immediately.
The session limit still applies. Simultaneous viewers
share one connection. Shutdown/unload cancels tasks and stops the owned monitor;
a session subsequently opened by the phone is not stopped.

## Implementation and limitations

MOVA/Dreame cloud identity → Aliyun identity → PIN-gated monitor → cloud WSS
signaling → **direct LAN UDP video** → HA's bundled ffmpeg → authenticated HA MJPEG.
Version 0.2.0 adopts the successful LAN PoC. The camera address is discovered from
the selected vacuum; it is not hardcoded. The receiver only accepts that private
IPv4 host, disables STUN/TURN media servers, and has no cloud-video relay fallback.
If direct video cannot be established, startup fails and the owned session is stopped.

The native HA camera entity/dialog and Start/Stop buttons are preserved. HA delivers
video at 640 pixels wide and 3 fps. Images and session keys exist only in memory and
are cleared at stop. No microphone, audio playback, recording, driving or cleaning
commands are implemented. Frames have size bounds; packet gaps discard the affected
frame and trigger a throttled keyframe request. Missing UDP data ends the session.

Camera attributes report `video_transport: direct_lan`, `lan_peer`,
`received_datagrams`, `media_cleanup_confirmed` and `cloud_setup_required: true`.
The last peer/count remain available after stop as transport evidence; image data
is cleared. `cleanup_confirmed` covers the owned monitor; media cleanup is separate.

### What is still needed for fully offline operation?

The direct video path is solved. Stock firmware currently still relies on cloud
services for account/IoT authentication, PIN-gated monitor start, signed signaling
and SDP/ICE key exchange, plus monitor keepalive and stop. The robot's advertised
LAN API has not yet been shown to provide those camera operations.

A fully offline implementation needs a local authenticated monitor-control route
and a local way to exchange the offer/answer and fresh ICE credentials. Those
credentials also supply the current packet-decryption key. Merely caching a cloud
login token or signed URL does not establish fresh sessions offline.

Further work: identify and test a stock-firmware LAN endpoint for these operations;
if none exists, assess whether firmware access can expose a local authenticated
camera service. Firmware modification/rooting has not been attempted. Finally,
validate cold startup, restart, PIN handling, live view and stop with WAN access
blocked. No firewall settings or firmware were changed for this adoption, and WAN
outage survival is unverified. The integration opens no RTMP relay; the robot's
own independent cloud traffic has not been audited. This release is **LAN video with cloud-assisted setup**.

Stock-LAN research on 2026-10-15 found a working CoAP device-discovery service
on Winky (UDP 5683, `/sys/device/info/get`). However, SDK discovery and the ALCS
authentication endpoint returned CoAP 4.04, including an authentication attempt
using legitimately issued credentials whose device identity matched the LAN
response. No authenticated local camera-control or signaling route was established.
The presence of ALCS client code in MOVAhome does not prove those firmware handlers
are enabled. This bounded result does not rule out other stock-firmware routes.

The integration depends on private `dreame_vacuum` cloud APIs and pinned aioice
internals. Only the LinkVisual/Aliyun provider is implemented. Winky's
`mova.vacuum.r2475a` is the verified model; other models are unverified.

## Development

Twenty-two tests cover media framing/decryption, LAN-only peer policy, malformed input,
a real UDP/ICE handshake, legacy framing, session ownership, cleanup, preview behavior
and PIN form handling. Run them in the Home Assistant environment with the declared
dependencies installed:

```sh
python -m unittest discover -s tests -v
```

See NOTICE for the MIT-licensed nodedreame protocol implementation used as the
reference for this Python port. No user credentials or camera images belong in
this repository.

## LAN adoption validation (0.2.0)

On 2026-10-15, the full integration manager sustained 60 seconds of Winky video
inside HA Core: 181 decoded 640×356 JPEG frames (3 fps), 15,294 LAN datagrams,
and confirmed media and monitor cleanup after viewer disconnect. Twenty-two tests
passed in Core, including a real UDP/ICE handshake. A separate receiver test decoded
513 source frames over 35 seconds without errors.

The cloud stream query and LAN start must both request stream type 1; mismatched
stream types produced video that stopped after a few seconds. Signaling uses the
session-derived client ID expected by the native SDK. ffmpeg uses wall-clock input
timestamps so its output tracks the requested 3 fps.

The final build was deployed and HA Core restarted successfully. The existing
Winky badge opened HA's native camera More Info dialog and displayed live video;
the deployed entity advanced to 174 decoded frames and 14,820 direct-LAN datagrams.
Closing the dialog returned the camera to Off with zero viewers and both cleanup
flags true. An off-state image request returned HTTP 503 without restarting the
camera. The final HA configuration check passed.

## Earlier cloud installation validation (0.1.0)

Verified in Felix’s HA OS instance on 2026-10-14 with Winky
(`mova.vacuum.r2475a`). PIN saved through HA integration options; independent
startup authenticated and decoded live video without MOVAhome's viewer open.
The HA camera API returned a 640×356 JPEG, and the native HA app displayed the
MJPEG live feed. Frame count advanced to 213 during the viewer test.

Start and Stop button entities worked. Stop confirmed monitor cleanup, cleared
the cached image, and left no integration ffmpeg process. An off-state snapshot
returned HTTP 503 without waking the robot camera. The 30-second no-initial-viewer
shutdown and last-viewer disconnect shutdown both confirmed cleanup. The native
macOS HA app retained its MJPEG connection after closing the popup and navigating
within the app; a full page refresh disconnected it and triggered shutdown.
The maximum-duration timer is implemented but was not separately exercised in
this live test. Ten tests passed inside Core’s Python environment, and the final
`ha core check` passed.

## Dashboard example

Use the generated entities on any dashboard. For Winky:

```yaml
type: vertical-stack
cards:
  - type: picture-entity
    entity: camera.winky_live
    camera_view: live
    show_state: false
  - type: horizontal-stack
    cards:
      - type: button
        entity: button.winky_camera_start
        name: Start camera
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.winky_camera_start
      - type: button
        entity: button.winky_camera_stop
        name: Stop camera
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.winky_camera_stop
```
