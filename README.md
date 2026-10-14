# Dreame Live Camera

Experimental, Python-only Home Assistant custom integration for the physical
camera in compatible Dreame/MOVA vacuums. Uses the already configured
`dreame_vacuum` integration's authenticated cloud transport. No Node.js server,
external bridge, cloud password copy or robot firmware change is required.

## Install and configure

Copy `custom_components/dreame_live_camera` into Home Assistant's
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

MOVA/Dreame cloud identity → Aliyun identity → PIN-gated monitor → private-mode
LinkVisual RTMP → video-only FLV → HA's bundled ffmpeg → authenticated HA MJPEG.
Video is 640 pixels wide at 3 fps. Images exist only in memory and are cleared at
stop. No microphone, audio playback, recording, driving or cleaning commands are
implemented. The media receiver has message/frame size bounds and read timeouts.

This first version uses MJPEG rather than native H.264/WebRTC delivery. It depends
on private `dreame_vacuum` cloud APIs and may need adjustment after upstream
updates. Only the LinkVisual/Aliyun provider is implemented. The working local
PoC was verified on `mova.vacuum.r2475a`; other models are unverified.

## Development

Protocol tests run with standard Python. Ten tests cover protocol framing, timestamps, size bounds, a real local TCP handshake, session ownership, cleanup, preview behavior and PIN form handling. Lifecycle/platform tests additionally
require the Home Assistant environment:

```sh
python -m unittest discover -s tests -v
```

See NOTICE for the MIT-licensed nodedreame protocol implementation used as the
reference for this Python port. No user credentials or camera images belong in
this repository.

## Current installation validation

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
