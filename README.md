# DUO_HOTAS_switch1_translator

Turns two Thrustmaster T.16000M joysticks into one Nintendo Switch Pro Controller, emulated over Bluetooth by a Raspberry Pi 4 B. A headless port of [duo_t16000m_to_virtual_controller_translator](https://github.com/FireB1scuit/duo_t16000m_to_virtual_controller_translator), which does the same for a virtual Xbox 360 pad on Windows.

Status and open questions are in [plan.md](plan.md). The Bluetooth side has not been run yet.

## Setup on the Pi

These steps are untested; adjust as needed.

```
sudo apt install python3-venv libdbus-1-dev libglib2.0-dev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Plug both sticks into the Pi's USB-A ports.

## Running

Check the sticks first. This needs neither Bluetooth nor root:

```
.venv/bin/python main.py --debug   # print raw axis/button/hat changes
.venv/bin/python main.py --test    # print the resulting Switch sticks/buttons
```

The first run asks you to wiggle the left stick so it knows which is which. Redo it with `--recalibrate`, for example after swapping USB ports.

Then emulate the controller. nxbt needs root:

```
sudo .venv/bin/python main.py
```

On the Switch, open **Controllers → Change Grip/Order** and wait for the controller to appear. Later runs reconnect to the same Switch without that screen.

Under `sudo` the calibration and any custom mappings are stored in root's home (`/root/.config/HOSAS_Switch_Translator/`), separate from the ones `--test` uses as the normal user. Run `sudo .venv/bin/python main.py --recalibrate` once so root has its own.

## Files

| File | Role |
| --- | --- |
| [config_store.py](config_store.py) | The Default mapping and custom mapping profiles (JSON, one per file). |
| [stick_engine.py](stick_engine.py) | Joystick discovery, left/right calibration, the polling thread, and `compute_virtual_state()`, which turns raw input plus a mapping into Switch sticks and buttons. |
| [nxbt_backend.py](nxbt_backend.py) | Sends that state to the Switch as a Pro Controller over Bluetooth. |
| [main.py](main.py) | The command line entry point. |

## Default mapping

Carried over from the Windows translator by position: the button that was at the bottom of the Xbox diamond (A) is the bottom of the Switch diamond (B), and so on.

| T.16000M input | Left stick → | Right stick → |
| --- | --- | --- |
| Trigger (button 0) | ZL | ZR |
| Button 1 | A | B |
| Button 2 | X | R |
| Button 3 | L | Y |
| Button 4 | Capture | Capture |
| Hat up / down / left / right | D-Pad | X / B / Y / A |
| Hat diagonal (↙) | Left stick click | Right stick click |
| Button 5 | X | X |
| Button 6 | Home | Home |
| Button 7 | Y | Y |
| Button 8 | B | B |
| Button 9 | A | A |
| Button 10 | Minus | Minus |
| Button 12 | Plus | Plus |
| Buttons 11, 13, 14, 15 | D-Pad up, right, down, left | same |

To change it, copy the Default mapping's JSON under `~/.config/HOSAS_Switch_Translator/configs/` to a new name, edit it, and run with `--config NAME`.
