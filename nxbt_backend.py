"""
Output backend: an emulated Switch Pro Controller over Bluetooth, via nxbt
(https://github.com/Brikwerk/nxbt). Linux only, and nxbt needs root because
it reconfigures BlueZ.

nxbt is imported lazily so --test/--debug work without it installed.
"""

STICK_RANGE = 100  # nxbt stick values run -100..100, +y is up



class NxbtBackend:
    def __init__(self, status_cb=None):
        import nxbt

        status_cb = status_cb or (lambda msg: None)
        self._nx = nxbt.Nxbt()

        # Reconnect to a Switch we've paired with before if there is one;
        # otherwise the Switch has to be on the pairing screen.
        known = self._nx.get_switch_addresses()
        if known:
            status_cb("Reconnecting to a previously paired Switch...")
        else:
            status_cb('On the Switch, open Controllers -> "Change Grip/Order" to pair...')
        self._index = self._nx.create_controller(nxbt.PRO_CONTROLLER, reconnect_address=known or None)
        self._nx.wait_for_connection(self._index)
        status_cb("Connected.")

        self._packet = self._nx.create_input_packet()
        self._button_keys = [k for k, v in self._packet.items() if isinstance(v, bool)]
        self._last = None

    def send(self, vs):
        """vs is the dict from stick_engine.compute_virtual_state()."""
        sticks = tuple(round(vs[k] * STICK_RANGE) for k in ("lx", "ly", "rx", "ry"))
        current = (sticks, frozenset(vs["active_buttons"]))
        # nxbt holds the last input until told otherwise, and each update is
        # an IPC round trip, so only send changes.
        if current == self._last:
            return
        self._last = current

        packet = self._packet
        active = vs["active_buttons"]
        for key in self._button_keys:
            packet[key] = key in active
        packet["L_STICK"]["X_VALUE"], packet["L_STICK"]["Y_VALUE"] = sticks[0], sticks[1]
        packet["R_STICK"]["X_VALUE"], packet["R_STICK"]["Y_VALUE"] = sticks[2], sticks[3]
        # Mapping targets share nxbt's key names; the stick clicks are the one
        # exception, nested under the stick rather than top-level.
        for stick in ("L_STICK", "R_STICK"):
            packet[stick]["PRESSED"] = stick in active
        self._nx.set_controller_input(self._index, packet)

    def close(self):
        self._nx.remove_controller(self._index)
