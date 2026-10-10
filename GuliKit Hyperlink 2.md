# GuliKit Hyperlink 2

Hardware notes for the wireless controller adapter used in this project. Everything under "Observed on the PC" was read from Windows on 2026-10-10; anything not observed is listed under "Not yet verified".

## What it is

A USB wireless controller adapter (dongle) made by GuliKit. A controller pairs to it wirelessly, and the adapter presents that controller to the USB host as a wired gamepad.

## Observed on the PC

Host: Windows 11 Pro, adapter plugged directly into a motherboard USB port, one controller paired.

| Field | Value |
|---|---|
| Windows friendly name | Xbox 360 Controller for Windows |
| Bus-reported device description | `GuliKit X` |
| VID:PID | `045E:028E` (Microsoft Xbox 360 wired controller identity) |
| Device class | `XnaComposite` (XInput) |
| Instance ID | `USB\VID_045E&PID_028E\000001` |
| HID child | `HID\VID_045E&PID_028E&IG_01` — "HID-compliant game controller", bus-reported as `Controller (GuliKit X)` |
| Status | OK, stock Windows driver, no extra driver installed |

## Behaviour that will trip you up

- **It is invisible until a controller is paired.** With the adapter plugged in but no controller connected, it does not enumerate on USB at all: no device entry, and no "Unknown USB Device" error entry either. This was the same on the USB hub and on a motherboard port. An empty device scan does not mean the adapter is broken or the cable is bad.
- **The PC sees a generic Xbox 360 pad, not the real controller.** The paired controller is hidden behind the `045E:028E` identity, so its make and model cannot be detected from the PC side.
- **Do not identify it by VID:PID alone.** `045E:028E` is shared with real Xbox 360 pads and with virtual pads created by ViGEm (the Nefarius Virtual Gamepad Emulation Bus is installed on this machine). Match on the bus-reported description `GuliKit X` instead.
- **Input reaches the PC through XInput.** The `IG_01` marker on the HID child means the HID interface is the XInput-backed one; read it with XInput rather than raw HID.

## Other devices on the same machine

Useful for telling the adapter apart from the rest of the setup.

| Device | VID:PID | Notes |
|---|---|---|
| Thrustmaster T.16000M joystick (x2) | `044F:B10A` | The two HOTAS sticks; both report the same VID:PID, distinguish by instance ID |
| TP-Link UB500 Bluetooth 5.4 adapter | `2357:0604` | Separate Bluetooth dongle, unrelated to the Hyperlink 2 |
| Generic USB 2.0 hub | `1A40:0101` | |
| Nefarius Virtual Gamepad Emulation Bus | `ROOT\SYSTEM\0002` | ViGEm, software only |

## Not yet verified

- Which controller was paired when the readings above were taken.
- Whether the adapter has other modes (for example a Switch mode) and what VID:PID and name it reports in them. The `GuliKit X` name suggests the observed mode is the XInput one, but no other mode has been seen.
- How the adapter appears to a Nintendo Switch.
- Button and axis mapping as reported through XInput.

## How to re-check

Run in PowerShell with a controller paired:

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.InstanceId -like 'USB\*' -or $_.InstanceId -like 'HID\*' } |
  ForEach-Object {
    [pscustomobject]@{
      Name = $_.FriendlyName
      Desc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' -ErrorAction SilentlyContinue).Data
      Id   = $_.InstanceId
    }
  } | Sort-Object Id | Format-Table -AutoSize
```

Look for the row whose `Desc` is `GuliKit X`.
