"""
Manages joystick-mapping config profiles.

There are two kinds of config:
  - "Default": the factory mapping, embedded in this file as a Python literal
    so it always exists even in a frozen .exe with no data files bundled. It
    is regenerated on disk any time it's missing and the app refuses to let
    it be edited or deleted.
  - Custom configs: user-created copies stored as JSON files, freely editable.

Everything lives under %APPDATA%/HOSAS_Translator so it survives regardless
of where the app/exe is run from and doesn't need write access next to the
executable (which may be read-only, e.g. under Program Files).
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_NAME = "Default"

# The mapping that shipped as config.json before this became a multi-profile
# app. Treated as the immutable factory default.
DEFAULT_CONFIG = {
    "stick_x_axis": 0,
    "stick_y_axis": 1,
    "invert_y_left": True,
    "invert_y_right": False,
    "trigger_button": 0,
    "stick_deadzone": 0.05,
    "poll_hz": 125,
    "left_buttons": {
        "1": "B",
        "2": "Y",
        "3": "LEFT_SHOULDER",
        "5": "Y",
        "6": "GUIDE",
        "7": "X",
        "8": "A",
        "9": "B",
        "10": "BACK",
        "11": "DPAD_UP",
        "12": "START",
        "13": "DPAD_RIGHT",
        "14": "DPAD_DOWN",
        "15": "DPAD_LEFT",
    },
    "right_buttons": {
        "1": "A",
        "2": "RIGHT_SHOULDER",
        "3": "X",
        "5": "Y",
        "6": "GUIDE",
        "7": "X",
        "8": "A",
        "9": "B",
        "10": "BACK",
        "11": "DPAD_UP",
        "12": "START",
        "13": "DPAD_RIGHT",
        "14": "DPAD_DOWN",
        "15": "DPAD_LEFT",
    },
    "left_hat": {
        "0,1": "DPAD_UP",
        "0,-1": "DPAD_DOWN",
        "1,0": "DPAD_RIGHT",
        "-1,0": "DPAD_LEFT",
        "-1,-1": "LEFT_THUMB",
    },
    "right_hat": {
        "0,1": "Y",
        "0,-1": "A",
        "1,0": "B",
        "-1,0": "X",
        "-1,-1": "RIGHT_THUMB",
    },
}

# Maps the button names used in config files to vgamepad's XUSB_BUTTON enum.
BUTTON_NAME_MAP = {
    "A": "XUSB_GAMEPAD_A",
    "B": "XUSB_GAMEPAD_B",
    "X": "XUSB_GAMEPAD_X",
    "Y": "XUSB_GAMEPAD_Y",
    "LEFT_SHOULDER": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "RIGHT_SHOULDER": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "DPAD_UP": "XUSB_GAMEPAD_DPAD_UP",
    "DPAD_DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "DPAD_LEFT": "XUSB_GAMEPAD_DPAD_LEFT",
    "DPAD_RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
    "LEFT_THUMB": "XUSB_GAMEPAD_LEFT_THUMB",
    "RIGHT_THUMB": "XUSB_GAMEPAD_RIGHT_THUMB",
    "BACK": "XUSB_GAMEPAD_BACK",
    "START": "XUSB_GAMEPAD_START",
    "GUIDE": "XUSB_GAMEPAD_GUIDE",
}

UNMAPPED = "(unmapped)"

# Every non-center direction a hat can report, in a stable display order.
HAT_DIRECTIONS = [
    (0, 1, "Up"),
    (0, -1, "Down"),
    (-1, 0, "Left"),
    (1, 0, "Right"),
    (-1, 1, "Up-Left"),
    (1, 1, "Up-Right"),
    (-1, -1, "Down-Left"),
    (1, -1, "Down-Right"),
]

# T.16000M buttons other than the trigger (button 0, which is hardcoded to
# LT/RT and isn't part of left_buttons/right_buttons) that the hardware
# reports. Used to build the editor's button-mapping table.
MAPPABLE_BUTTON_INDICES = list(range(1, 20))


def app_data_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    path = base / "HOSAS_Translator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configs_dir():
    path = app_data_dir() / "configs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def calibration_path():
    return app_data_dir() / "stick_calibration.json"


def _default_config_path():
    return configs_dir() / f"{DEFAULT_CONFIG_NAME}.json"


def ensure_default_config():
    """(Re)writes the factory default to disk so it's always present."""
    path = _default_config_path()
    with open(path, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)
    return path


def is_protected(name):
    return name == DEFAULT_CONFIG_NAME


def list_config_names():
    ensure_default_config()
    names = sorted(
        p.stem for p in configs_dir().glob("*.json") if p.stem != DEFAULT_CONFIG_NAME
    )
    return [DEFAULT_CONFIG_NAME] + names


def load_config(name):
    if name == DEFAULT_CONFIG_NAME:
        ensure_default_config()
        return dict(DEFAULT_CONFIG)
    path = configs_dir() / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No config named {name!r}")
    with open(path, "r") as f:
        config = json.load(f)
    merged = {**DEFAULT_CONFIG, **config}
    return merged


def save_custom_config(name, config):
    if is_protected(name):
        raise ValueError("The Default config is read-only; save under a different name.")
    if not name or any(c in name for c in '\\/:*?"<>|'):
        raise ValueError("Config name is empty or contains invalid filename characters.")
    validate_config(config)
    path = configs_dir() / f"{name}.json"
    with open(path, "w") as f:
        json.dump(config, f, indent=4)
    return path


def delete_custom_config(name):
    if is_protected(name):
        raise ValueError("The Default config cannot be deleted.")
    path = configs_dir() / f"{name}.json"
    if path.exists():
        path.unlink()


def _state_path():
    return app_data_dir() / "state.json"


def _load_state():
    try:
        with open(_state_path(), "r") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def get_setting(key, default=None):
    """A value remembered between runs (see set_setting)."""
    return _load_state().get(key, default)


def set_setting(key, value):
    state = _load_state()
    state[key] = value
    with open(_state_path(), "w") as f:
        json.dump(state, f)


def get_last_active_config():
    name = get_setting("active_config", DEFAULT_CONFIG_NAME)
    return name if name in list_config_names() else DEFAULT_CONFIG_NAME


def set_last_active_config(name):
    set_setting("active_config", name)


def validate_config(config):
    missing = [key for key in DEFAULT_CONFIG if key not in config]
    if missing:
        raise ValueError(f"Config is missing keys: {', '.join(missing)}")
    for section in ("left_buttons", "right_buttons"):
        for name in config[section].values():
            if name not in BUTTON_NAME_MAP:
                raise ValueError(f"Unknown button name {name!r} in {section}")
    for section in ("left_hat", "right_hat"):
        for name in config[section].values():
            if name not in BUTTON_NAME_MAP:
                raise ValueError(f"Unknown button name {name!r} in {section}")
