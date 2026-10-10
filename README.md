# duo_t16000m_to_virtual_controller_translator

- [x] read out joystick values
- [x] virtual controller
- [x] left joystick xy to wasd
- [x] right joystick xy to mouse xy
- [x] triggers to LT and RT
- [x] map joystick buttons
- [x] map joystick hat to arrows and XABY
- [x] Windows GUI app with debug view, recalibration, and custom mapping profiles
- [ ] map throttle button (slider left = mouse sensitivity, slider right = volume)
- [ ] map extra button on base
- [ ] map horizontal X rotation for X joystick acceleration

## Setup guide

1. Install the [ViGEmBus driver](https://github.com/ViGEm/ViGEmBus/releases) — this lets `vgamepad` create a virtual Xbox 360 controller on Windows.
2. Plug in both T.16000M joysticks.
   - If a game also reacts to the physical sticks, you might need HidHide too — see [Optional: hiding the physical sticks from games](#optional-hiding-the-physical-sticks-from-games-hidhide).
3. Either grab `HOSAS Translator.exe` from a build (see [Building the .exe](#building-the-exe)) and run it, or run from source:
   ```
   pip install -r requirements.txt
   python gui.py
   ```

### Optional: hiding the physical sticks from games (HidHide)

The translator adds a virtual Xbox 360 controller, but it does not remove the two real T.16000Ms — Windows still shows them to every program. A game that supports joysticks itself can then read both the sticks and the virtual controller at once, which may show up as input that drops out, fights itself, or is bound twice. If you see that, you might need [HidHide](https://github.com/nefarius/HidHide/releases) (free, from the author of ViGEmBus) to hide the sticks from everything except the translator. Windows has no way to do this without a driver like HidHide, so the translator can't do it on its own.

One-time setup:

1. Install HidHide and reboot when asked.
2. Open **HidHide Configuration Client** (it asks for administrator rights).
3. **Applications** tab: click **+** and add the `HOSAS Translator.exe` you actually run (or your `python.exe` if you run from source). Only programs listed here can still see hidden devices.
4. **Devices** tab: tick both T.16000M entries, then tick **Enable device hiding**.
5. Restart any program that already had the sticks open (the game, Steam), or reboot.

Things to know:

- The application list is by file path — if you move or rename the `.exe`, add the new path again or the translator itself will stop finding the sticks.
- Hiding is global: while it's enabled, no other game or tool sees the sticks directly. Untick **Enable device hiding** when you want to use them without the translator.

### Using the app

- **Control** — shows connection status, lets you pick the active mapping, and has the Start/Stop button that turns the virtual Xbox 360 controller on and off.
- **Debug** — a live Xbox 360 controller schematic (ABXY in their real colors, D-pad, bumpers/triggers, sticks, Back/Guide/Start) that lights up as you press things, so you can see exactly what's reaching the virtual controller. Below it, the raw per-stick axis/button/hat values plus each pressed button's mapped target (e.g. `1→B`), useful for confirming your hardware's indices.
- **Calibration** — both T.16000M units report identical hardware IDs with no serial, so left/right is determined once by wiggling the left stick; the result is cached so this normally only runs the first time. If you swap which stick is in which USB port, use **Recalibrate** here.
- **Configs** — browse the built-in **Default** mapping (read-only) and any custom mappings you've made. **New from Default** clones it into an editable copy; the editor lets you point-and-click a target Xbox button for every stick button and hat direction, plus axis indices, deadzone, poll rate, and Y-inversion — no JSON editing needed. **Set as Active** switches which mapping the Control tab's virtual controller uses.

All of this is stored per-user under `%APPDATA%\HOSAS_Translator\` (the Default mapping, your custom mappings, the USB-port calibration cache, and which mapping was last active), so it's independent of where the app is installed or run from.

### Command line

The same engine is also usable headless, e.g. for scripting or quick hardware checks:

```
python main.py --test          # print processed stick/trigger values
python main.py --debug         # print raw axis/button/hat changes as they happen
python main.py --recalibrate   # redo the left/right wiggle test
python main.py --config NAME   # use a custom mapping instead of Default
python main.py                 # run the translator (Ctrl+C to stop)
```

## Project structure

| File | Role |
| --- | --- |
| [config_store.py](config_store.py) | The Default mapping (embedded, read-only, always regenerated if missing) and custom mapping profiles, stored under `%APPDATA%\HOSAS_Translator\`. |
| [stick_engine.py](stick_engine.py) | Hardware layer: joystick discovery, left/right calibration + its cache, the background polling thread (`StickManager`), and `compute_virtual_state()` — the one function that turns raw input + a config into processed stick/trigger values and active button names, used by everything else so they can't disagree. |
| [controller_view.py](controller_view.py) | The Canvas-drawn Xbox 360 controller schematic shown on the Debug tab. |
| [gui.py](gui.py) | The Windows app — this is what `build.ps1` packages into the .exe. |
| [main.py](main.py) | The headless CLI (`--test` / `--debug` / `--recalibrate` / `--config`), built on the same `config_store.py` / `stick_engine.py`. |
| [build.ps1](build.ps1) | Packages `gui.py` into `dist\HOSAS Translator.exe` via PyInstaller. |

## Building the .exe

```
pip install -r requirements-dev.txt
.\build.ps1
```

This produces a single-file `dist\HOSAS Translator.exe` with `--noconsole` (no terminal window) that bundles the Python interpreter, so the target machine only needs the ViGEmBus driver installed, not Python. Since it's an unsigned homebrew build, Windows SmartScreen may ask you to confirm "Run anyway" the first time.

`build.ps1` also explicitly bundles vgamepad's `ViGEmClient.dll` as data (`--add-data`) — vgamepad loads it from a path relative to its own package folder at runtime rather than through a normal Python import, so PyInstaller's automatic dependency analysis doesn't find it on its own. Without that, the exe runs fine but clicking **Start** fails with `Failed to load dynlib/dll ... ViGEmClient.dll`.

If a rebuild fails with a `PermissionError` on `dist\HOSAS Translator.exe`, it's because a previous build of the exe is still running (Windows locks the file while it's open) — close it and rebuild.

## Button mapping

The Default mapping (`DEFAULT_CONFIG` in [config_store.py](config_store.py)) maps buttons as follows; `BUTTON_NAME_MAP` in the same file lists every supported target name, and the Configs tab in the app lets you build your own layout without editing code.

![Button mapping diagram](button_mapping.svg)

| T.16000M input | Left stick → | Right stick → |
| --- | --- | --- |
| Trigger (button 0) | LT | RT |
| Button 1 | B | A |
| Button 2 | Y | RB |
| Button 3 | LB | X |
| Hat up | D-Pad up | Y |
| Hat down | D-Pad down | A |
| Hat left | D-Pad left | X |
| Hat right | D-Pad right | B |
| Hat diagonal (↖) | Left stick click (L3) | Right stick click (R3) |
| Button 5 | Y | Y |
| Button 6 | Guide (Xbox button) | Guide (Xbox button) |
| Button 7 | X | X |
| Button 8 | A | A |
| Button 9 | B | B |
| Button 10 | Back | Back |
| Button 11 | D-Pad up | D-Pad up |
| Button 12 | Start | Start |
| Button 13 | D-Pad right | D-Pad right |
| Button 14 | D-Pad down | D-Pad down |
| Button 15 | D-Pad left | D-Pad left |
| Button 4, 16–19 | unmapped | unmapped |

Button 4 (screenshot/share) has no Xbox 360 equivalent — `VX360Gamepad` emulates a 360 pad, which predates that button.

![Reference image](reference.png)
