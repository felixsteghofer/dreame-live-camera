# 0.3.0

- Prefer direct LAN video and automatically try the cloud relay if LAN signaling,
  media or decoding fails, including a connection that produces no first frame.
- Close the failed receiver and decoder before switching transports. Retain the
  original session limit, viewer cleanup and protection of a phone-owned session.
- Report `cloud_relay`, relay byte counts and a safe fallback reason in camera
  attributes. Signed URLs, PINs and video are never logged or saved.
- Restart Home Assistant after installing this update. Existing settings and
  entity IDs are preserved.

# 0.2.1

- Fix shutdown cleanup running on the wrong thread by registering an async Home Assistant stop listener.
- Await the existing camera cleanup without starting a camera session or changing entity IDs.
- Restart Home Assistant after installing this update. Existing settings are preserved.
