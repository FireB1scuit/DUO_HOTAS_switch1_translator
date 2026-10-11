"""
Reads the two T.16000M joysticks and (optionally) drives a virtual Xbox 360
pad from them. Used by both the CLI (main.py) and the GUI (gui.py).

StickManager owns a single background thread that polls pygame at
config["poll_hz"] and, while a gamepad is active, feeds a virtual controller
from the same reads - the GUI's debug view and its "activate controller"
toggle are just two consumers of that one loop, not two separate pollers.
"""

import ctypes
import gc
import glob
import json
import os
import sys
import threading
import time

# SDL otherwise installs its own SIGINT/SIGTERM handlers on Linux and turns
# them into queued quit events nobody here reads, so Ctrl+C and `kill` would
# be silently ignored. Read at pygame.init() time, so setting it here (before
# any caller gets that far) is early enough.
os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")

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


def list_stick_ports():
    """Linux only: [(usb_port, event_device)] for every plugged-in stick,
    sorted by port, e.g. [("1.3", "/dev/input/event7"), ("1.4", ...)].

    The port is the hub/port chain the kernel reports, so it names a physical
    USB socket - the only thing that tells two T.16000Ms apart. Empty on
    other platforms."""
    if not sys.platform.startswith("linux"):
        return []
    found = []
    for name_file in glob.glob("/sys/class/input/event*/device/name"):
        device_dir = os.path.dirname(name_file)
        try:
            with open(name_file) as f:
                name = f.read()
            with open(os.path.join(device_dir, "phys")) as f:
                phys = f.read().strip()
        except OSError:
            continue
        if STICK_NAME_HINT not in name:
            continue
        # phys looks like "usb-0000:01:00.0-1.4/input0"
        port = phys.split("/")[0].rsplit("-", 1)[-1]
        found.append((port, "/dev/input/" + os.path.basename(os.path.dirname(device_dir))))
    return sorted(found)


# USB port of each stick returned by the last discover_joysticks(), in the
# same order; empty when the ports aren't known (Windows).
_stick_ports = []


def discover_joysticks():
    global _stick_ports
    ports = list_stick_ports()
    if ports:
        # SDL opens the devices named here first and in this order, which
        # pins the joystick order to the USB ports instead of to whichever
        # stick the kernel happened to enumerate first.
        os.environ["SDL_JOYSTICK_DEVICE"] = ":".join(device for _, device in ports)
    pygame.joystick.quit()
    pygame.joystick.init()
    all_joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    sticks = [j for j in all_joysticks if STICK_NAME_HINT in j.get_name()]
    _stick_ports = [port for port, _ in ports] if len(ports) == len(sticks) else []
    return sticks, all_joysticks


def stick_port(joysticks, joy):
    """USB port of joy (one of the sticks from the last discovery), or None."""
    if len(_stick_ports) != len(joysticks):
        return None
    return _stick_ports[joysticks.index(joy)]


def load_cached_left_index(joysticks):
    """Index into joysticks of the stick calibrated as left, or None. The
    USB port wins when it's known; the bare index is the fallback."""
    path = config_store.calibration_path()
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if len(_stick_ports) == len(joysticks) and cache.get("left_port") in _stick_ports:
        return _stick_ports.index(cache["left_port"])
    return cache.get("left_index")


def save_cached_left(joysticks, left):
    cache = {"left_index": joysticks.index(left)}
    port = stick_port(joysticks, left)
    if port is not None:
        cache["left_port"] = port
    with open(config_store.calibration_path(), "w") as f:
        json.dump(cache, f)


def wait_for_active_stick(joysticks, threshold=0.5, timeout=15, axes=(0, 1), progress_cb=None, should_abort=None):
    """Blocks until one of joysticks moves past threshold on axes, or timeout.

    Returns the moved joystick, or None on timeout/abort. Intended to be run
    off the calling thread's UI loop when used from the GUI.
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
    calls (new process, or GUI recalibrate=False) skip the movement wait."""
    status_cb = status_cb or (lambda msg: None)

    if not recalibrate:
        cached_index = load_cached_left_index(joysticks)
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
    save_cached_left(joysticks, left)
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
    per that stick's button/hat mapping. Names, not vgamepad enum values, so
    this has no dependency on vgamepad and can be reused for display."""
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
    virtual controller would show: processed stick axes, LT/RT, and the set
    of active button target names. The single source of truth for both
    GamepadController (what's actually sent) and the debug view (what's
    shown), so they can't drift apart."""
    lx = apply_deadzone(safe_axis(left_state["axes"], config["stick_x_axis"]), config["stick_deadzone"])
    ly = apply_deadzone(safe_axis(left_state["axes"], config["stick_y_axis"]), config["stick_deadzone"])
    rx = apply_deadzone(safe_axis(right_state["axes"], config["stick_x_axis"]), config["stick_deadzone"])
    ry = apply_deadzone(safe_axis(right_state["axes"], config["stick_y_axis"]), config["stick_deadzone"])
    if config["invert_y_left"]:
        ly = -ly
    if config["invert_y_right"]:
        ry = -ry

    trig_index = config["trigger_button"]
    lt = 1.0 if safe_button(left_state["buttons"], trig_index) else 0.0
    rt = 1.0 if safe_button(right_state["buttons"], trig_index) else 0.0

    active_buttons = _active_target_names(left_state, config["left_buttons"], config["left_hat"])
    active_buttons |= _active_target_names(right_state, config["right_buttons"], config["right_hat"])

    return {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": lt, "rt": rt, "active_buttons": active_buttons}


class GamepadController:
    """Wraps a vgamepad VX360Gamepad, mapping raw stick state to it per a
    config dict. Import of vgamepad is deferred so modules that only need
    read access (e.g. the debug view) work without ViGEmBus installed."""

    def __init__(self, config):
        import vgamepad as vg

        self._vg = vg
        self.gamepad = vg.VX360Gamepad()
        # compute_virtual_state() speaks XInput: +Y is stick up. vgamepad's
        # Linux backend writes the value straight to evdev, where +Y is down,
        # so without this flip both sticks come out upside down there.
        self._y_sign = 1.0 if sys.platform == "win32" else -1.0
        self.set_config(config)

    def set_config(self, config):
        self.config = config
        mapped_names = (
            set(config["left_buttons"].values())
            | set(config["right_buttons"].values())
            | set(config["left_hat"].values())
            | set(config["right_hat"].values())
        )
        self.all_buttons = {
            getattr(self._vg.XUSB_BUTTON, config_store.BUTTON_NAME_MAP[name]) for name in mapped_names
        }

    def update(self, left_state, right_state):
        vs = compute_virtual_state(left_state, right_state, self.config)

        self.gamepad.left_joystick_float(x_value_float=vs["lx"], y_value_float=vs["ly"] * self._y_sign)
        self.gamepad.right_joystick_float(x_value_float=vs["rx"], y_value_float=vs["ry"] * self._y_sign)
        self.gamepad.left_trigger_float(value_float=vs["lt"])
        self.gamepad.right_trigger_float(value_float=vs["rt"])

        active_vg = {
            getattr(self._vg.XUSB_BUTTON, config_store.BUTTON_NAME_MAP[name]) for name in vs["active_buttons"]
        }
        for vg_button in self.all_buttons:
            if vg_button in active_vg:
                self.gamepad.press_button(button=vg_button)
            else:
                self.gamepad.release_button(button=vg_button)

        self.gamepad.update()

    def close(self):
        self.gamepad.reset()
        self.gamepad.update()
        # On Linux the uinput device only goes away when the pad object is
        # freed, and vgamepad's object sits in a reference cycle - without a
        # collection the "stopped" controller stays visible to games.
        self.gamepad = None
        gc.collect()


class StickManager:
    """Owns the two joystick handles and a background polling thread.

    Call connect() once sticks are found, then start_polling(). GUI code
    reads get_snapshot() from the Tk main loop via after(); it must not
    touch pygame directly from another thread.
    """

    def __init__(self):
        self.left = None
        self.right = None
        self._sticks = []
        self.config = dict(config_store.DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self._state = {"left": None, "right": None}
        self._thread = None
        self._running = False
        self._gamepad = None
        self._gamepad_lock = threading.Lock()
        self.gamepad_active = False
        self.last_error = None

    # -- connection -------------------------------------------------
    def find_sticks(self):
        sticks, all_joysticks = discover_joysticks()
        return sticks, all_joysticks

    def assign(self, sticks, recalibrate=False, status_cb=None, progress_cb=None, should_abort=None):
        left, right, used_cache = identify_sticks(
            sticks, recalibrate=recalibrate, status_cb=status_cb, progress_cb=progress_cb, should_abort=should_abort
        )
        with self._lock:
            self._sticks = list(sticks)
            self.left, self.right = left, right
        return used_cache

    def swap_sticks(self):
        """Makes the other stick the left one and remembers it, without
        having to stop polling."""
        with self._lock:
            self.left, self.right = self.right, self.left
            save_cached_left(self._sticks, self.left)

    def stick_ports(self):
        """(left, right) USB ports, each None where unknown."""
        with self._lock:
            if not self._sticks or self.left is None:
                return None, None
            return stick_port(self._sticks, self.left), stick_port(self._sticks, self.right)

    def is_connected(self):
        return self.left is not None and self.right is not None

    def set_config(self, config):
        with self._lock:
            self.config = config
        with self._gamepad_lock:
            if self._gamepad is not None:
                self._gamepad.set_config(config)

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
        self.stop_gamepad()
        set_high_res_timer(False)

    def _poll_loop(self):
        ticker = Ticker(self.config.get("poll_hz", 125))
        while self._running:
            try:
                pygame.event.pump()
                with self._lock:
                    left, right = self.left, self.right
                left_raw = read_raw_state(left)
                right_raw = read_raw_state(right)
                with self._lock:
                    self._state = {
                        "left": {"name": left.get_name(), **left_raw},
                        "right": {"name": right.get_name(), **right_raw},
                    }
                with self._gamepad_lock:
                    if self.gamepad_active and self._gamepad is not None:
                        self._gamepad.update(left_raw, right_raw)
            except Exception as exc:  # keep the loop alive; surface the error to the GUI
                self.last_error = str(exc)
            ticker.wait()

    def get_snapshot(self):
        with self._lock:
            return dict(self._state)

    # -- virtual controller -------------------------------------------
    def start_gamepad(self):
        with self._gamepad_lock:
            if self._gamepad is None:
                self._gamepad = GamepadController(self.config)
            self.gamepad_active = True

    def stop_gamepad(self):
        with self._gamepad_lock:
            self.gamepad_active = False
            if self._gamepad is not None:
                self._gamepad.close()
                self._gamepad = None
