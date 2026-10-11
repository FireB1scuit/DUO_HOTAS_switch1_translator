"""
CLI entry point: combines two Thrustmaster T.16000M joysticks into a single
emulated Nintendo Switch Pro Controller, sent over Bluetooth from a
Raspberry Pi: left stick -> left stick, right stick -> right stick, and the
buttons/hats per the active mapping (see config_store.py).

Run with --test to print processed stick/button values, or --debug to print
raw axis/button/hat changes, instead of emulating a controller - useful for
confirming the axis/button indices in a config match your hardware. Neither
needs Bluetooth or root. Add --recalibrate to redo the left/right
identification, or --config NAME to use a saved custom mapping.

Without --test/--debug this needs nxbt and root (see README.md).
"""

import sys
import time

import pygame

import config_store
from stick_engine import StickManager, Ticker, compute_virtual_state


def parse_args(argv):
    config_name = config_store.DEFAULT_CONFIG_NAME
    if "--config" in argv:
        idx = argv.index("--config")
        try:
            config_name = argv[idx + 1]
        except IndexError:
            sys.exit("--config requires a config name")
    return {
        "test": "--test" in argv,
        "debug": "--debug" in argv,
        "recalibrate": "--recalibrate" in argv,
        "config_name": config_name,
    }


def run_test_mode(manager):
    print("Test mode: printing values, Ctrl+C to stop.")
    ticker = Ticker(manager.config["poll_hz"])
    while True:
        snap = manager.get_snapshot()
        vs = compute_virtual_state(snap["left"], snap["right"], manager.config)
        buttons = ",".join(sorted(vs["active_buttons"])) or "none"
        print(
            f"L=({vs['lx']:+.2f},{vs['ly']:+.2f})   "
            f"R=({vs['rx']:+.2f},{vs['ry']:+.2f})   buttons={buttons:<40}",
            end="\r",
        )
        ticker.wait()


def run_debug_mode(manager):
    print("Debug mode: printing every changing raw axis/button/hat, Ctrl+C to stop.")
    ticker = Ticker(manager.config["poll_hz"])
    prev = manager.get_snapshot()
    while True:
        state = manager.get_snapshot()
        for name in ("left", "right"):
            old, new = prev[name], state[name]
            for i, (o, n) in enumerate(zip(old["axes"], new["axes"])):
                if abs(n - o) > 0.01:
                    print(f"{name} axis {i}: {o:+.3f} -> {n:+.3f}")
            for i, (o, n) in enumerate(zip(old["buttons"], new["buttons"])):
                if n != o:
                    print(f"{name} button {i}: {o} -> {n}")
            if old["hat"] != new["hat"]:
                print(f"{name} hat: {old['hat']} -> {new['hat']}")
        prev = state
        ticker.wait()


def run_controller_mode(manager):
    from nxbt_backend import NxbtBackend

    manager.start_output(NxbtBackend(status_cb=print))
    print("Emulated Pro Controller active. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.5)
            if manager.last_error:
                print(f"Error: {manager.last_error}")
                manager.last_error = None
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    args = parse_args(sys.argv[1:])

    pygame.init()

    manager = StickManager()
    sticks, all_joysticks = manager.find_sticks()
    if len(sticks) != 2:
        names = ", ".join(j.get_name() for j in all_joysticks) or "none"
        sys.exit(f"Expected 2 T.16000M sticks, found {len(sticks)}. Detected devices: {names}")

    try:
        config = config_store.load_config(args["config_name"])
    except FileNotFoundError:
        sys.exit(f"No config named {args['config_name']!r}. Omit --config to use Default.")
    manager.set_config(config)
    manager.assign(sticks, recalibrate=args["recalibrate"], status_cb=print)

    manager.start_polling()
    try:
        if args["debug"]:
            run_debug_mode(manager)
        elif args["test"]:
            run_test_mode(manager)
        else:
            run_controller_mode(manager)
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop_polling()


if __name__ == "__main__":
    main()
