"""
Linux-only: a small web page for running the Switch bridge from a phone.

Serves portal.html on the local network and does what switch_bridge.py
does, but controllable while it runs: connect/disconnect the emulated Pro
Controller, put it in pairing mode, choose which USB port is the left
stick, and edit the button mappings. Everything is saved under
~/.config/HOSAS_Translator/ (the same files the GUI uses), so it comes
back the way it was left - including reconnecting to the Switch on start.

Must run as root, like switch_bridge.py (NXBT reconfigures BlueZ):

    sudo env HOME=$HOME .venv/bin/python web_portal.py [--port 8080] [--no-switch]

--no-switch skips Bluetooth entirely (no root needed): the page still shows
the sticks and edits mappings, which is enough to set things up or test.

There is no login: anyone on the network can reach the page. Use --host to
bind it to one address if that matters.
"""

import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no display when run under sudo/systemd

import pygame

import config_store
from stick_engine import StickManager, compute_virtual_state, list_stick_ports
from switch_bridge import XBOX_TO_SWITCH, build_packet, start_pairing_agent

PAGE_PATH = Path(__file__).with_name("portal.html")
MAX_BODY_BYTES = 64 * 1024
IDENTIFY_SECONDS = 15
# The sticks report over USB every 10 ms; reading them much more often than
# the mapping configs' 125 Hz keeps the wait for the next read short.
STICK_POLL_HZ = 500
IDENTIFY_THRESHOLD = 0.5
NEUTRAL_STATE = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "active_buttons": set()}

BUTTON_KEY = re.compile(r"\d{1,2}")
HAT_KEY = re.compile(r"-?[01],-?[01]")


def clean_config(raw):
    """Rebuilds a config sent by the page from known keys only, with the
    types the engine expects, so a bad request can't leave a profile on disk
    that breaks the polling loop."""
    if not isinstance(raw, dict):
        raise ValueError("Config must be an object.")
    config = {}
    for key, default in config_store.DEFAULT_CONFIG.items():
        value = raw.get(key, default)
        if isinstance(default, dict):
            pattern = HAT_KEY if key.endswith("_hat") else BUTTON_KEY
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and pattern.fullmatch(k) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"Bad mapping in {key}.")
            value = dict(value)
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        else:
            value = float(value)
        config[key] = value
    if not 0 <= config["stick_deadzone"] <= 0.9:
        raise ValueError("Deadzone must be between 0 and 0.9.")
    if not 10 <= config["poll_hz"] <= 1000:
        raise ValueError("Poll rate must be between 10 and 1000 Hz.")
    config_store.validate_config(config)
    return config


def config_names():
    # Not config_store.list_config_names(): that rewrites Default.json on
    # every call, and the page asks for this several times a second.
    names = sorted(p.stem for p in config_store.configs_dir().glob("*.json") if not config_store.is_protected(p.stem))
    return [config_store.DEFAULT_CONFIG_NAME] + names


def fix_ownership():
    """As root, files created here would be root-owned and the GUI (run as
    the normal user) could no longer write them - hand them back to whoever
    owns the folder above."""
    if os.geteuid() != 0:
        return
    root = config_store.app_data_dir()
    owner = root.parent.stat()
    for path in [root, *root.rglob("*")]:
        try:
            os.chown(path, owner.st_uid, owner.st_gid)
        except OSError:
            pass


class Portal:
    """Owns the sticks and the Switch link. One worker thread does all the
    pygame discovery and NXBT calls; the HTTP threads only change what is
    wanted and read back the status."""

    def __init__(self, nx):
        self.nx = nx
        self.manager = StickManager()
        self._lock = threading.Lock()
        self._running = True

        self.active_name = config_store.get_last_active_config()
        self.manager.set_config(config_store.load_config(self.active_name))

        self._stick_ports = None  # (port, device) of each stick when connected
        self.sticks_message = "Looking for the sticks..."
        self._rescan = False
        self._identify_until = None

        # "off", "connect" (reconnect to a known Switch) or "pair"
        self.wanted = "connect" if nx and config_store.get_setting("switch_autoconnect", False) else "off"
        self.link_state = "off"
        self.link_error = None
        self.link_address = None
        self.known_switches = []
        self._known_at = None
        self._index = None
        self._restart_link = False
        self._retry_at = 0.0
        self._last_packet = None

    # -- worker -------------------------------------------------------
    def run(self):
        next_check = 0.0
        while self._running:
            # Wake the moment the poll thread sees the sticks change, rather
            # than sampling its result on a second timer; the timeout only
            # keeps the checks below running while nothing moves.
            self.manager.changed.wait(timeout=0.02)
            self.manager.changed.clear()
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 0.25
                self._check_sticks()
                try:
                    self._check_link(now)
                except Exception as exc:
                    self.link_error = str(exc)
                    self._forget_controller()
                    self._retry_at = now + 2
            self._check_identify(now)
            try:
                self._send_input()
            except Exception as exc:
                self.link_error = str(exc)

    def stop(self):
        self._running = False

    def shutdown(self):
        self.manager.stop_polling()
        if self._index is not None:
            try:
                self.nx.remove_controller(self._index)
            except Exception:
                pass

    def _check_sticks(self):
        ports = list_stick_ports()
        if self.manager.is_connected():
            if ports == self._stick_ports and not self._rescan:
                return
            # Unplugged or moved: the pygame handles are dead either way.
            self.manager.stop_polling()
            self.manager.left = self.manager.right = None
            self._identify_until = None
        self._rescan = False
        if len(ports) != 2:
            self.sticks_message = f"Found {len(ports)} of 2 T.16000M sticks - plug both in."
            return
        try:
            sticks, _ = self.manager.find_sticks()
            if len(sticks) != 2:
                self.sticks_message = f"Could not open both sticks (got {len(sticks)})."
                return
            # Never block on the wiggle test here: with nothing saved yet,
            # take the ports in order and let the page swap them.
            self.manager.assign(sticks, should_abort=lambda: True)
            fix_ownership()
            self.manager.start_polling(poll_hz=STICK_POLL_HZ)
        except Exception as exc:
            self.sticks_message = f"Could not open the sticks: {exc}"
            return
        self._stick_ports = ports
        self.sticks_message = "Both sticks connected."

    def _check_identify(self, now):
        if self._identify_until is None:
            return
        if now >= self._identify_until or not self.manager.is_connected():
            self._identify_until = None
            return
        snap = self.manager.get_snapshot()
        for side in ("left", "right"):
            state = snap.get(side)
            if state and any(abs(value) > IDENTIFY_THRESHOLD for value in state["axes"][:2]):
                if side == "right":
                    self.manager.swap_sticks()
                    fix_ownership()
                self._identify_until = None
                self.sticks_message = "Left stick set - that port is saved as left."
                return

    def _forget_controller(self):
        index, self._index = self._index, None
        self._last_packet = None
        self.link_address = None
        if index is not None:
            try:
                self.nx.remove_controller(index)
            except Exception:
                pass

    def _check_link(self, now):
        if self.nx is None:
            return
        import nxbt

        with self._lock:
            wanted = self.wanted
            restart, self._restart_link = self._restart_link, False
        if self._known_at is None or now - self._known_at > 10:
            # Kept fresh even while off, so the page can offer the choice
            # of Switch before connecting.
            self.known_switches = list(self.nx.get_switch_addresses())
            self._known_at = now
        if wanted == "off":
            self._forget_controller()
            self.link_state = "off"
            return
        if restart and self._index is not None:
            # Let NXBT tear the old controller down before the next one
            # claims the adapter.
            self._forget_controller()
            self._retry_at = now + 1
            self.link_state = "starting"
            return

        if self._index is None:
            if now < self._retry_at:
                return
            addresses = None
            if wanted == "connect":
                selected = config_store.get_setting("switch_address")
                addresses = [selected] if selected in self.known_switches else self.known_switches
            self.link_state = "starting"
            self._index = self.nx.create_controller(nxbt.PRO_CONTROLLER, reconnect_address=addresses or None)
            return

        info = self.nx.state[self._index]
        state = info["state"]
        if state == "crashed":
            self.link_error = str(info.get("errors")).strip().splitlines()[-1]
            self._forget_controller()
            self._retry_at = now + 2
            self.link_state = "starting"
            return
        if state == "connected" and self.link_state != "connected":
            self.link_error = None
            self.link_address = info.get("last_connection")
            self.known_switches = list(self.nx.get_switch_addresses())
            with self._lock:
                if self.wanted == "pair":
                    # Paired: from here on this is the Switch to come back to.
                    self.wanted = "connect"
                    config_store.set_setting("switch_address", self.link_address)
                    config_store.set_setting("switch_autoconnect", True)
                    fix_ownership()
        self.link_state = state

    def _send_input(self):
        if self._index is None or self.link_state != "connected":
            return
        vs = NEUTRAL_STATE
        # No input while a stick is being wiggled for identification, and
        # none from the stale last read after a stick is unplugged.
        if self.manager.is_connected() and self._identify_until is None:
            snap = self.manager.get_snapshot()
            if snap["left"] is not None and snap["right"] is not None:
                vs = compute_virtual_state(snap["left"], snap["right"], self.manager.config)
        packet = build_packet(self.nx, vs)
        if packet != self._last_packet:
            self.nx.set_controller_input(self._index, packet)
            self._last_packet = packet

    # -- called from HTTP threads ---------------------------------------
    def status(self):
        connected = self.manager.is_connected()
        sticks = {"connected": connected, "message": self.sticks_message}
        if connected:
            snap = self.manager.get_snapshot()
            ports = dict(zip(("left", "right"), self.manager.stick_ports()))
            for side in ("left", "right"):
                state = snap.get(side)
                if state is None:
                    continue
                sticks[side] = {
                    "port": ports[side],
                    "x": round(state["axes"][0], 3) if state["axes"] else 0,
                    "y": round(state["axes"][1], 3) if len(state["axes"]) > 1 else 0,
                    "buttons": [i for i, pressed in enumerate(state["buttons"]) if pressed],
                    "hat": list(state["hat"]),
                }
        identify_until = self._identify_until
        return {
            "sticks": sticks,
            "identify": max(0, round(identify_until - time.monotonic())) if identify_until else 0,
            "switch": {
                "available": self.nx is not None,
                "wanted": self.wanted,
                "state": self.link_state,
                "error": self.link_error,
                "address": self.link_address,
                "known": self.known_switches,
                "selected": config_store.get_setting("switch_address"),
            },
            "active_config": self.active_name,
            "configs": config_names(),
        }

    def get_config(self, name):
        self._require_existing(name)
        return {
            "name": name,
            "read_only": config_store.is_protected(name),
            "config": config_store.load_config(name),
        }

    def _require_existing(self, name):
        if name not in config_names():
            raise ValueError(f"No mapping named {name!r}.")

    def post(self, path, data):
        handler = {
            "/api/switch": self._post_switch,
            "/api/sticks": self._post_sticks,
            "/api/active": self._post_active,
            "/api/config": self._post_config,
            "/api/config/delete": self._post_delete,
        }.get(path)
        if handler is None:
            raise LookupError(path)
        try:
            handler(data)
        finally:
            fix_ownership()
        return self.status()

    def _post_switch(self, data):
        if self.nx is None:
            raise ValueError("Started with --no-switch: Bluetooth is off.")
        action = data.get("action")
        with self._lock:
            if action in ("connect", "pair"):
                if action != self.wanted or self.link_state != "connected":
                    self._restart_link = True
                self.wanted = action
                self.link_error = None
                if action == "connect":
                    config_store.set_setting("switch_autoconnect", True)
            elif action == "disconnect":
                self.wanted = "off"
                config_store.set_setting("switch_autoconnect", False)
            elif action == "select":
                address = data.get("address") or None
                if address is not None and address not in self.known_switches:
                    raise ValueError("That Switch is not paired with this machine.")
                config_store.set_setting("switch_address", address)
                if self.wanted == "connect" and address not in (None, self.link_address):
                    self._restart_link = True
            else:
                raise ValueError("Unknown action.")

    def _post_sticks(self, data):
        action = data.get("action")
        if action == "rescan":
            self._rescan = True
            return
        if not self.manager.is_connected():
            raise ValueError("The sticks are not connected.")
        if action == "swap":
            self.manager.swap_sticks()
            self._identify_until = None
            self.sticks_message = "Swapped - saved for next time."
        elif action == "identify":
            self._identify_until = time.monotonic() + IDENTIFY_SECONDS
        elif action == "cancel":
            self._identify_until = None
        else:
            raise ValueError("Unknown action.")

    def _post_active(self, data):
        name = data.get("name")
        self._require_existing(name)
        self.manager.set_config(config_store.load_config(name))
        self.active_name = name
        config_store.set_last_active_config(name)

    def _post_config(self, data):
        """Saves a custom mapping; with "activate" it also becomes the one in use."""
        name = data.get("name")
        if not isinstance(name, str):
            raise ValueError("Missing mapping name.")
        name = name.strip()
        if data.get("create") and name in config_names():
            raise ValueError(f"A mapping named {name!r} already exists.")
        config_store.save_custom_config(name, clean_config(data.get("config")))
        if data.get("activate") or name == self.active_name:
            self._post_active({"name": name})

    def _post_delete(self, data):
        name = data.get("name")
        self._require_existing(name)
        config_store.delete_custom_config(name)
        if name == self.active_name:
            self._post_active({"name": config_store.DEFAULT_CONFIG_NAME})


class Handler(BaseHTTPRequestHandler):
    portal = None

    def log_message(self, format, *args):
        pass  # the page polls constantly; the default per-request log is noise

    def _send(self, code, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if url.path == "/":
                self._send(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self._send(200, self.portal.status())
            elif url.path == "/api/meta":
                self._send(200, {
                    "targets": list(config_store.BUTTON_NAME_MAP),
                    "switch_names": XBOX_TO_SWITCH,
                    "buttons": config_store.MAPPABLE_BUTTON_INDICES,
                    "hat_directions": [[x, y, label] for x, y, label in config_store.HAT_DIRECTIONS],
                })
            elif url.path == "/api/config":
                name = parse_qs(url.query).get("name", [""])[0]
                self._send(200, self.portal.get_config(name))
            else:
                self._send(404, {"error": "Not found."})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self):
        # Only the page itself may change things: a JSON body can't be sent
        # cross-site without a preflight, and a foreign Origin is refused.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._send(403, {"error": "Cross-site request refused."})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "Expected application/json."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("Bad request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            self._send(200, self.portal.post(urlsplit(self.path).path, data))
        except LookupError:
            self._send(404, {"error": "Not found."})
        except (ValueError, OSError) as exc:
            self._send(400, {"error": str(exc)})


def local_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
            return probe.getsockname()[0]
    except OSError:
        return socket.gethostname()


def option(argv, flag, default):
    if flag not in argv:
        return default
    try:
        return argv[argv.index(flag) + 1]
    except IndexError:
        sys.exit(f"{flag} requires a value")


def main():
    argv = sys.argv[1:]
    host = option(argv, "--host", "0.0.0.0")
    port = int(option(argv, "--port", "8080"))
    use_switch = "--no-switch" not in argv
    if use_switch and os.geteuid() != 0:
        sys.exit("Run as root: sudo env HOME=$HOME .venv/bin/python web_portal.py   (or add --no-switch)")

    nx = agent = None
    if use_switch:
        # NXBT forks its worker processes; that has to happen before pygame
        # and any thread exist (see switch_bridge.py).
        import nxbt

        nx = nxbt.Nxbt()
        agent = start_pairing_agent()

    pygame.init()
    portal = Portal(nx)
    Handler.portal = portal
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=portal.run, daemon=True)
    worker.start()

    def on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)  # systemd stop: take the same clean path as Ctrl+C
    shown_host = local_address() if host == "0.0.0.0" else host
    print(f"Portal running: http://{shown_host}:{port}  (or http://{socket.gethostname()}.local:{port})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        portal.stop()
        worker.join(timeout=3)
        portal.shutdown()
        if agent is not None:
            agent.terminate()


if __name__ == "__main__":
    main()
