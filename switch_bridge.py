"""
Linux-only: sends the T.16000M sticks to a Nintendo Switch over
Bluetooth, by making this machine pose as a Pro Controller (via NXBT)
instead of creating a local virtual Xbox 360 pad.

Uses the same mapping configs as the GUI/CLI: compute_virtual_state() does
the stick/button mapping, and the Xbox target names are translated to
Switch buttons here (see XBOX_TO_SWITCH).

Must run as root (NXBT reconfigures BlueZ and opens the HID L2CAP ports):

    sudo env HOME=$HOME .venv/bin/python switch_bridge.py [--config NAME] [--recalibrate] [--switch MAC]

With only one stick plugged in it runs single-stick mode: that stick is the
whole controller (see config_store.MODE_SINGLE).

First pairing: open Controllers -> Change Grip/Order on the Switch. Later
runs try to reconnect to the already-paired Switch first.
With more than one paired
Switch, --switch picks which one to reconnect to.
"""

import multiprocessing
import os
import sys
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no display when run under sudo

import pygame

import config_store
from stick_engine import StickManager, Ticker

# Config target name (Xbox naming) -> NXBT input packet key. Face buttons
# map by position, not by label: Nintendo swaps A/B and X/Y relative to
# Xbox, so the Xbox bottom button (A) has to press the Switch's bottom
# button (B), and so on.
XBOX_TO_SWITCH = {
    "A": "B",
    "B": "A",
    "X": "Y",
    "Y": "X",
    "LEFT_SHOULDER": "L",
    "RIGHT_SHOULDER": "R",
    "DPAD_UP": "DPAD_UP",
    "DPAD_DOWN": "DPAD_DOWN",
    "DPAD_LEFT": "DPAD_LEFT",
    "DPAD_RIGHT": "DPAD_RIGHT",
    "BACK": "MINUS",
    "START": "PLUS",
    "GUIDE": "HOME",
    "CAPTURE": "CAPTURE",
}

AGENT_PATH = "/hosas/agent"


def _run_pairing_agent():
    """The Switch pairs "just works", but BlueZ still wants an agent to
    confirm it - without one every pairing attempt times out. bluetoothd
    forgets its agents whenever it restarts (NXBT restarts it), so this
    re-registers each time org.bluez gets a new owner."""
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    class Agent(dbus.service.Object):
        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            pass

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus(private=True)
    agent = Agent(bus, AGENT_PATH)  # noqa: F841 - must stay referenced while the loop runs

    def register():
        try:
            manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
            manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
            manager.RequestDefaultAgent(AGENT_PATH)
        except dbus.DBusException:
            pass  # already registered, or bluetoothd mid-restart (retried on its next owner change)
        return False

    bus.add_signal_receiver(
        lambda name, old, new: new and GLib.timeout_add(500, register),
        "NameOwnerChanged", "org.freedesktop.DBus", arg0="org.bluez",
    )
    register()
    GLib.MainLoop().run()


def start_pairing_agent():
    """Runs the agent in its own process: its GLib main loop would otherwise
    share NXBT's D-Bus connection across threads, which deadlocks NXBT's
    blocking BlueZ calls."""
    process = multiprocessing.Process(target=_run_pairing_agent, daemon=True)
    process.start()
    return process


def build_packet(nx, vs):
    packet = nx.create_input_packet()
    packet["L_STICK"]["X_VALUE"] = round(vs["lx"] * 100)
    packet["L_STICK"]["Y_VALUE"] = round(vs["ly"] * 100)
    packet["R_STICK"]["X_VALUE"] = round(vs["rx"] * 100)
    packet["R_STICK"]["Y_VALUE"] = round(vs["ry"] * 100)
    packet["L_STICK"]["PRESSED"] = "LEFT_THUMB" in vs["active_buttons"]
    packet["R_STICK"]["PRESSED"] = "RIGHT_THUMB" in vs["active_buttons"]
    packet["ZL"] = vs["lt"] > 0.5
    packet["ZR"] = vs["rt"] > 0.5
    for name in vs["active_buttons"]:
        key = XBOX_TO_SWITCH.get(name)
        if key:
            packet[key] = True
    return packet


def main():
    argv = sys.argv[1:]
    if os.geteuid() != 0:
        sys.exit("Run as root: sudo env HOME=$HOME .venv/bin/python switch_bridge.py")
    config_name = argv[argv.index("--config") + 1] if "--config" in argv else config_store.DEFAULT_CONFIG_NAME

    # NXBT forks its worker processes; that has to happen before pygame and
    # the polling thread exist, or the forked children can deadlock on locks
    # copied mid-use from those threads.
    import nxbt

    nx = nxbt.Nxbt()
    agent = start_pairing_agent()

    pygame.init()
    manager = StickManager()
    sticks, all_joysticks = manager.find_sticks()
    if len(sticks) not in (1, 2):
        names = ", ".join(j.get_name() for j in all_joysticks) or "none"
        sys.exit(f"Expected 1 or 2 T.16000M sticks, found {len(sticks)}. Detected devices: {names}")
    print("Two sticks: duo mode." if len(sticks) == 2 else "One stick: single-stick mode.", flush=True)
    manager.set_config(config_store.load_config(config_name))
    manager.assign(sticks, recalibrate="--recalibrate" in argv, status_cb=lambda m: print(m, flush=True))
    manager.start_polling()

    only_switch = argv[argv.index("--switch") + 1] if "--switch" in argv else None

    def connect():
        known = [only_switch] if only_switch else nx.get_switch_addresses()
        print(
            "Waiting for the Switch" + (f" (known: {', '.join(known)})" if known else "")
            + " - if it doesn't connect, open Controllers -> Change Grip/Order.",
            flush=True,
        )
        return nx.create_controller(nxbt.PRO_CONTROLLER, reconnect_address=known or None)

    index = connect()
    ticker = Ticker(manager.config["poll_hz"])
    state = last_state = last_packet = None
    next_state_check = 0.0
    try:
        while True:
            # nx.state and set_controller_input() are round trips to NXBT's
            # manager process, which its controller loop also depends on -
            # so check the link only a few times a second and send input
            # only when it changed (NXBT keeps replaying the last packet).
            now = time.monotonic()
            if now >= next_state_check:
                next_state_check = now + 0.25
                state = nx.state[index]["state"]
                if state != last_state:
                    print(f"{time.strftime('%H:%M:%S')} Switch link: {state}", flush=True)
                    last_state, last_packet = state, None
                if state == "crashed":
                    print(str(nx.state[index].get("errors")).strip().splitlines()[-1], flush=True)
                    nx.remove_controller(index)
                    time.sleep(2)
                    index = connect()
                    continue
            if state == "connected":
                vs = manager.virtual_state()
                if vs is not None:
                    packet = build_packet(nx, vs)
                    if packet != last_packet:
                        nx.set_controller_input(index, packet)
                        last_packet = packet
            ticker.wait()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        manager.stop_polling()
        try:
            nx.remove_controller(index)
        except Exception:
            pass
        agent.terminate()


if __name__ == "__main__":
    main()
