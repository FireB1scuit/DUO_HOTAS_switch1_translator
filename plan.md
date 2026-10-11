# Plan

Goal: two Thrustmaster T.16000M sticks plugged into a Raspberry Pi 4 B, showing up on a Nintendo Switch 1 as one controller, wirelessly, without buying more hardware.

```
2x T.16000M --USB--> Pi 4 B --Bluetooth, posing as a Pro Controller--> [Hyperlink 2 --USB-->] Switch
```

The Hyperlink 2 is a Bluetooth receiver for controllers (it accepts Switch Pro, DualShock 4, DualSense and Xbox pads), so the Pi has to pose as one of those. The same emulated Pro Controller can also pair with the Switch directly, with no adapter in between.

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | Port the Windows translator's engine to run headless (no GUI, no ViGEm), with Switch button names | Code written. Mapping logic and poll loop checked on Windows; not yet run on the Pi or with the sticks attached |
| 2 | Pro Controller over Bluetooth via nxbt, paired directly with the Switch | Code written (`nxbt_backend.py`), never run |
| 3 | Pair the same emulated controller with the Hyperlink 2 instead of the Switch | Not started. Unknown whether the Hyperlink accepts it |
| 4 | Start at boot (systemd service), reconnect without a terminal | Not started |

## Open questions

- **Does the Hyperlink 2 pair with an emulated Pro Controller?** nxbt was written against the Switch's own pairing handshake. Nobody appears to have tried it against a GuliKit adapter. Answered by stage 3.
- **Is the Pi 4's onboard Bluetooth stable enough?** Some users of similar projects report dropouts. Answered by playing for a while in stage 2.
- **Do the T.16000M's axis and button numbers match between Windows and Linux?** The Default mapping uses the Windows numbering. Check with `python main.py --debug` on the Pi.
- **Is left/right stable across reboots?** Both sticks are identical with no serial, and the cached assignment is by enumeration order. If it flips, identify sticks by USB port instead.

## Fallbacks

- **Direct Bluetooth to the Switch** (stage 2 as the end state). Needs no extra hardware; the Hyperlink just goes unused.
- **Wired.** The Pi 4's USB-A ports are host-only, so USB-A to USB-A does not work. Its USB-C port can act as a USB device: a USB-C to USB-A cable into the dock, with the Pi posing as a wired controller. That port is also the Pi's power input, so the dock would have to power the Pi and both sticks. Would need a new output backend next to `nxbt_backend.py`.
