"""
Reads the two T.16000M joysticks and (optionally) drives an emulated Switch
controller from them. Used by the CLI (main.py).

StickManager owns a single background thread that polls pygame at
config["poll_hz"] and, while an output backend is attached, feeds it from
the same reads. A backend is any object with send(virtual_state) and
close() - see nxbt_backend.py.
"""

import ctypes
import json
import os
import sys
import threading
import time

# The Pi runs headless: SDL needs a (dummy) video driver for its event loop,
# and must deliver joystick events without a focused window. Set before
# pygame is imported.
if sys.platform != "win32":
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")

import pygame

import config_store

STICK_NAME_HINT = "T.16000M"


class Ticker:
    """Sleeps to hold a constant poll rate, compensating for time spent each iteration."""

    def __init__(self, hz):
        self.period = 1 / hz
        self.next_tick = time.perf_counter() + self.period

    def wait(self):
        now = time.perf_counter()
        delay = self.next_tick - now
        if delay > 0:
            time.sleep(delay)
        else:
            self.next_tick = now
        self.next_tick += self.period


def set_high_res_timer(enable):
    if sys.platform == "win32":
        (ctypes.windll.winmm.timeBeginPeriod if enable else ctypes.windll.winmm.timeEndPeriod)(1)


def apply_deadzone(value, deadzone):
    magnitude = abs(value)
    if magnitude < deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    scaled = (magnitude - deadzone) / (1.0 - deadzone)
    return sign * min(scaled, 1.0)


def discover_joysticks():
    pygame.joystick.quit()
    pygame.joystick.init()
    all_joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    return [j for j in all_joysticks if STICK_NAME_HINT in j.get_name()], all_joysticks


def load_cached_left_index():
    path = config_store.calibration_path()
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f).get("left_index")
    except (json.JSONDecodeError, OSError):
        return None


def save_cached_left_index(left_index):
    with open(config_store.calibration_path(), "w") as f:
        json.dump({"left_index": left_index}, f)


def wait_for_active_stick(joysticks, threshold=0.5, timeout=15, axes=(0, 1), progress_cb=None, should_abort=None):
    """Blocks until one of joysticks moves past threshold on axes, or timeout.

    Returns the moved joystick, or None on timeout/abort. Intended to be run
    off any UI thread.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_abort and should_abort():
            return None
        pygame.event.pump()
        for joy in joysticks:
            if any(abs(joy.get_axis(a)) > threshold for a in axes):
                return joy
        if progress_cb:
            progress_cb(max(0, deadline - time.time()))
        time.sleep(0.02)
    return None


def identify_sticks(joysticks, recalibrate=False, status_cb=None, progress_cb=None, should_abort=None):
    """Returns (left, right, used_cache). Caches the winning slot so future
    calls (new process, or recalibrate=False) skip the movement wait."""
    status_cb = status_cb or (lambda msg: None)

    if not recalibrate:
        cached_index = load_cached_left_index()
        if cached_index is not None and 0 <= cached_index < len(joysticks):
            left = joysticks[cached_index]
            right = next(j for j in joysticks if j is not left)
            status_cb(f"Using cached assignment -> Left: {left.get_name()}, Right: {right.get_name()}")
            return left, right, True

    status_cb("Move the LEFT stick now (just the left one)...")
    left = wait_for_active_stick(joysticks, progress_cb=progress_cb, should_abort=should_abort)
    if left is None:
        status_cb("Calibration cancelled or timed out; defaulting to enumeration order.")
        left = joysticks[0]
    right = next(j for j in joysticks if j is not left)
    save_cached_left_index(joysticks.index(left))
    status_cb(f"Left  -> {left.get_name()}\nRight -> {right.get_name()}")
    return left, right, False


def read_raw_state(joy):
    return {
        "axes": [joy.get_axis(i) for i in range(joy.get_numaxes())],
        "buttons": [joy.get_button(i) for i in range(joy.get_numbuttons())],
        "hat": joy.get_hat(0) if joy.get_numhats() > 0 else (0, 0),
    }


def safe_axis(axes, index):
    return axes[index] if 0 <= index < len(axes) else 0.0


def safe_button(buttons, index):
    return bool(buttons[index]) if 0 <= index < len(buttons) else False


def _active_target_names(state, button_map, hat_map):
    """Target names (e.g. "B", "DPAD_UP") currently active on one stick,
    per that stick's button/hat mapping."""
    active = set()
    for index_str, name in button_map.items():
        if safe_button(state["buttons"], int(index_str)):
            active.add(name)
    hat_value = state["hat"]
    for direction_str, name in hat_map.items():
        x_str, y_str = direction_str.split(",")
        if (int(x_str), int(y_str)) == hat_value:
            active.add(name)
    return active


def compute_virtual_state(left_state, right_state, config):
    """Maps raw left/right stick state through a config into what the
    emulated controller should show: processed stick axes (-1..1, +y is up)
    and the set of active button target names. The single source of truth
    for both the output backend and --test, so they can't drift apart."""
    lx = apply_deadzone(safe_axis(left_state["axes"], config["stick_x_axis"]), config["stick_deadzone"])
    ly = apply_deadzone(safe_axis(left_state["axes"], config["stick_y_axis"]), config["stick_deadzone"])
    rx = apply_deadzone(safe_axis(right_state["axes"], config["stick_x_axis"]), config["stick_deadzone"])
    ry = apply_deadzone(safe_axis(right_state["axes"], config["stick_y_axis"]), config["stick_deadzone"])
    if config["invert_y_left"]:
        ly = -ly
    if config["invert_y_right"]:
        ry = -ry

    active_buttons = _active_target_names(left_state, config["left_buttons"], config["left_hat"])
    active_buttons |= _active_target_names(right_state, config["right_buttons"], config["right_hat"])

    return {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "active_buttons": active_buttons}


class StickManager:
    """Owns the two joystick handles and a background polling thread.

    Call assign() once sticks are found, then start_polling(). Other
    threads read get_snapshot(); they must not touch pygame directly.
    """

    def __init__(self):
        self.left = None
        self.right = None
        self.config = dict(config_store.DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self._state = {"left": None, "right": None}
        self._thread = None
        self._running = False
        self._output = None
        self._output_lock = threading.Lock()
        self.last_error = None

    # -- connection -------------------------------------------------
    def find_sticks(self):
        sticks, all_joysticks = discover_joysticks()
        return sticks, all_joysticks

    def assign(self, sticks, recalibrate=False, status_cb=None, progress_cb=None, should_abort=None):
        left, right, used_cache = identify_sticks(
            sticks, recalibrate=recalibrate, status_cb=status_cb, progress_cb=progress_cb, should_abort=should_abort
        )
        self.left, self.right = left, right
        return used_cache

    def is_connected(self):
        return self.left is not None and self.right is not None

    def set_config(self, config):
        with self._lock:
            self.config = config

    # -- polling ------------------------------------------------------
    def start_polling(self):
        if self._running:
            return
        self._running = True
        set_high_res_timer(True)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_polling(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.stop_output()
        set_high_res_timer(False)

    def _poll_loop(self):
        ticker = Ticker(self.config.get("poll_hz", 125))
        while self._running:
            try:
                pygame.event.pump()
                left_raw = read_raw_state(self.left)
                right_raw = read_raw_state(self.right)
                with self._lock:
                    config = self.config
                    self._state = {
                        "left": {"name": self.left.get_name(), **left_raw},
                        "right": {"name": self.right.get_name(), **right_raw},
                    }
                with self._output_lock:
                    if self._output is not None:
                        self._output.send(compute_virtual_state(left_raw, right_raw, config))
            except Exception as exc:  # keep the loop alive; surface the error to the caller
                self.last_error = str(exc)
            ticker.wait()

    def get_snapshot(self):
        with self._lock:
            return dict(self._state)

    # -- emulated controller ------------------------------------------
    def start_output(self, backend):
        with self._output_lock:
            self._output = backend

    def stop_output(self):
        with self._output_lock:
            if self._output is not None:
                self._output.close()
                self._output = None
