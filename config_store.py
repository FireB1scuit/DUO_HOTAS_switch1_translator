"""
Manages joystick-mapping config profiles.

There are two kinds of config:
  - "Default": the factory mapping, embedded in this file as a Python literal
    so it always exists. It
    is regenerated on disk any time it's missing and the app refuses to let
    it be edited or deleted.
  - Custom configs: user-created copies stored as JSON files, freely editable.

Everything lives under ~/.config/HOSAS_Switch_Translator (%APPDATA% on
Windows), so it is independent of where the code is checked out. Note that
under sudo "~" is root's home, not the pi user's.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_NAME = "Default"

# Factory default. Ported from the Windows (Xbox 360) translator by position,
# not by letter: the Switch has A/B and X/Y swapped relative to Xbox, so the
# button that sat at the bottom of the diamond there (Xbox A) is Switch B here.
DEFAULT_CONFIG = {
    "stick_x_axis": 0,
    "stick_y_axis": 1,
    "invert_y_left": True,
    "invert_y_right": False,
    "stick_deadzone": 0.05,
    "poll_hz": 125,
    "left_buttons": {
        "0": "ZL",
        "1": "A",
        "2": "X",
        "3": "L",
        "4": "CAPTURE",
        "5": "X",
        "6": "HOME",
        "7": "Y",
        "8": "B",
        "9": "A",
        "10": "MINUS",
        "11": "DPAD_UP",
        "12": "PLUS",
        "13": "DPAD_RIGHT",
        "14": "DPAD_DOWN",
        "15": "DPAD_LEFT",
    },
    "right_buttons": {
        "0": "ZR",
        "1": "B",
        "2": "R",
        "3": "Y",
        "4": "CAPTURE",
        "5": "X",
        "6": "HOME",
        "7": "Y",
        "8": "B",
        "9": "A",
        "10": "MINUS",
        "11": "DPAD_UP",
        "12": "PLUS",
        "13": "DPAD_RIGHT",
        "14": "DPAD_DOWN",
        "15": "DPAD_LEFT",
    },
    "left_hat": {
        "0,1": "DPAD_UP",
        "0,-1": "DPAD_DOWN",
        "1,0": "DPAD_RIGHT",
        "-1,0": "DPAD_LEFT",
        "-1,-1": "L_STICK",
    },
    "right_hat": {
        "0,1": "X",
        "0,-1": "B",
        "1,0": "A",
        "-1,0": "Y",
        "-1,-1": "R_STICK",
    },
}

# Every Switch Pro Controller button a mapping can target. L_STICK/R_STICK
# are the stick clicks.
BUTTON_NAMES = (
    "A", "B", "X", "Y",
    "L", "R", "ZL", "ZR",
    "MINUS", "PLUS", "HOME", "CAPTURE",
    "L_STICK", "R_STICK",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
)

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

# Button indices a T.16000M can report (0 is the trigger).
MAPPABLE_BUTTON_INDICES = list(range(0, 20))


def app_data_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    path = base / "HOSAS_Switch_Translator"
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


def get_last_active_config():
    path = _state_path()
    if not path.exists():
        return DEFAULT_CONFIG_NAME
    try:
        with open(path, "r") as f:
            name = json.load(f).get("active_config", DEFAULT_CONFIG_NAME)
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG_NAME
    return name if name in list_config_names() else DEFAULT_CONFIG_NAME


def set_last_active_config(name):
    with open(_state_path(), "w") as f:
        json.dump({"active_config": name}, f)


def validate_config(config):
    missing = [key for key in DEFAULT_CONFIG if key not in config]
    if missing:
        raise ValueError(f"Config is missing keys: {', '.join(missing)}")
    for section in ("left_buttons", "right_buttons"):
        for name in config[section].values():
            if name not in BUTTON_NAMES:
                raise ValueError(f"Unknown button name {name!r} in {section}")
    for section in ("left_hat", "right_hat"):
        for name in config[section].values():
            if name not in BUTTON_NAMES:
                raise ValueError(f"Unknown button name {name!r} in {section}")
