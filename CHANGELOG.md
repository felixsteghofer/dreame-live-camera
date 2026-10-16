# 0.2.1

- Fix shutdown cleanup running on the wrong thread by registering an async Home Assistant stop listener.
- Await the existing camera cleanup without starting a camera session or changing entity IDs.
- Restart Home Assistant after installing this update. Existing settings are preserved.
