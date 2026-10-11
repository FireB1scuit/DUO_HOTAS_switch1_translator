"""
GUI for the T.16000M -> virtual Xbox 360 controller translator (Windows,
and Linux via uinput - see README.md).

Left half is a tabbed panel:
  - Control: connect the sticks, pick which config is active, start/stop
    the virtual controller, and re-run left/right calibration if you swap
    USB ports.
  - Configs: browse the read-only Default mapping and any custom mappings,
    and edit custom ones with a point-and-click button/hat mapping table.

Right half is always the Debug panel: live raw + processed axis/button/hat
values for both sticks, stacked vertically (schematic on top, left stick
below it, right stick below that).

Built on config_store.py / stick_engine.py, which are also used by the
plain CLI in main.py.
"""

import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import pygame

import config_store
from controller_view import ControllerSchematic
from stick_engine import StickManager, compute_virtual_state

APP_TITLE = "HOSAS Translator"
WINDOW_WIDTH = 1300

if sys.platform == "win32":
    VIRTUAL_PAD_HINT = "Is ViGEmBus installed?"
else:
    VIRTUAL_PAD_HINT = "Does your user have write access to /dev/uinput? See the Linux section of README.md."


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable container. Put widgets in .body."""

    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)

        self.body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        # X11 has no <MouseWheel>; the wheel arrives as buttons 4 (up) / 5 (down).
        def bind_wheel(_event):
            canvas.bind_all("<MouseWheel>", on_mousewheel)
            canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        def unbind_wheel(_event):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(sequence)

        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x750")
        self.minsize(1150, 650)

        pygame.init()
        self.manager = StickManager()
        self._connect_lock = threading.Lock()
        self._abort_calibration = threading.Event()

        self.active_config_name = tk.StringVar(value=config_store.get_last_active_config())
        self.connection_status = tk.StringVar(value="Not connected")
        self.calibration_status = tk.StringVar(value="")
        self.controller_status = tk.StringVar(value="Virtual controller: stopped")

        self._build_ui()
        # Font metrics differ per platform (Linux fonts run taller than
        # Segoe UI), so grow the window to whatever the debug panel needs
        # rather than clipping its bottom rows.
        self.update_idletasks()
        self.geometry(f"{WINDOW_WIDTH}x{max(750, self.winfo_reqheight())}")
        self._load_active_config(persist=False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, lambda: self._connect(recalibrate=False))
        self.after(100, self._refresh_debug)

    # ------------------------------------------------------------ layout --
    def _build_ui(self):
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        notebook = ttk.Notebook(panes)
        self.debug_tab = ttk.Frame(panes, padding=16)
        panes.add(notebook, weight=1)
        panes.add(self.debug_tab, weight=1)

        self.control_tab = ttk.Frame(notebook, padding=16)
        self.configs_tab = ttk.Frame(notebook, padding=16)

        notebook.add(self.control_tab, text="Control")
        notebook.add(self.configs_tab, text="Configs")

        self._build_control_tab()
        self._build_debug_tab()
        self._build_configs_tab()

        # Give the debug side enough width for the schematic up front instead
        # of the 50/50 split the PanedWindow would otherwise start with. The
        # sash can only be placed once the pane has its real width, which on
        # X11 is later than the first idle callback - so wait for the resize.
        def place_sash(event):
            if event.width >= WINDOW_WIDTH - 50:
                panes.unbind("<Configure>")
                panes.sashpos(0, 480)

        panes.bind("<Configure>", place_sash)

    # ---------------------------------------------------------- control --
    def _build_control_tab(self):
        tab = self.control_tab

        conn = ttk.LabelFrame(tab, text="Sticks", padding=12)
        conn.pack(fill="x", pady=(0, 12))
        # Button first so it keeps its full width; the status text wraps in
        # whatever is left instead of pushing the button out of the frame.
        ttk.Button(conn, text="Connect / Refresh", command=lambda: self._connect(recalibrate=False)).pack(
            side="right", padx=(8, 0)
        )
        ttk.Label(conn, textvariable=self.connection_status, wraplength=270, justify="left").pack(side="left")

        cfg = ttk.LabelFrame(tab, text="Active mapping", padding=12)
        cfg.pack(fill="x", pady=(0, 12))
        self.active_config_combo = ttk.Combobox(
            cfg, textvariable=self.active_config_name, state="readonly", values=config_store.list_config_names()
        )
        self.active_config_combo.pack(side="left", fill="x", expand=True)
        self.active_config_combo.bind("<<ComboboxSelected>>", lambda e: self._load_active_config(persist=True))

        run = ttk.LabelFrame(tab, text="Virtual controller", padding=12)
        run.pack(fill="x", pady=(0, 12))
        ttk.Label(run, textvariable=self.controller_status).pack(side="left")
        self.toggle_button = ttk.Button(run, text="Start", command=self._toggle_controller, state="disabled")
        self.toggle_button.pack(side="right")

        cal = ttk.LabelFrame(tab, text="Calibration", padding=12)
        cal.pack(fill="x")
        ttk.Label(
            cal,
            text=(
                "Both sticks report identical hardware IDs, so left/right is determined by "
                "wiggling the left stick once. That result is cached, so this normally only "
                "runs the first time or after you change USB ports."
            ),
            wraplength=400,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.calibration_label = ttk.Label(cal, textvariable=self.calibration_status, font=("Segoe UI", 10))
        self.calibration_label.pack(anchor="w", pady=(0, 12))

        row = ttk.Frame(cal)
        row.pack(anchor="w")
        self.recalibrate_button = ttk.Button(row, text="Recalibrate", command=self._recalibrate)
        self.recalibrate_button.pack(side="left")
        self.abort_button = ttk.Button(row, text="Cancel", command=self._abort_calibration.set, state="disabled")
        self.abort_button.pack(side="left", padx=(8, 0))

    def _toggle_controller(self):
        if self.manager.gamepad_active:
            self.manager.stop_gamepad()
            self.controller_status.set("Virtual controller: stopped")
            self.toggle_button.config(text="Start")
            return
        try:
            self.manager.start_gamepad()
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, f"Couldn't create the virtual controller:\n{exc}\n\n{VIRTUAL_PAD_HINT}"
            )
            return
        self.controller_status.set("Virtual controller: active")
        self.toggle_button.config(text="Stop")

    def _reset_controller_ui(self):
        self.controller_status.set("Virtual controller: stopped")
        self.toggle_button.config(text="Start")

    # ------------------------------------------------------------ debug --
    def _build_debug_tab(self):
        tab = self.debug_tab

        canvas_wrap = ttk.Frame(tab)
        canvas_wrap.pack(fill="x", pady=(0, 12))
        self.schematic = ControllerSchematic(canvas_wrap)
        self.schematic.pack()

        self.debug_widgets = {}
        for side in ("left", "right"):
            frame = ttk.LabelFrame(tab, text=side.capitalize(), padding=12)
            frame.pack(fill="x", pady=(0, 12))

            name_label = ttk.Label(frame, text="Not connected", font=("Segoe UI", 9, "bold"))
            name_label.pack(anchor="w")

            processed_label = ttk.Label(frame, text="x=+0.00 y=+0.00  trigger=False")
            processed_label.pack(anchor="w", pady=(6, 6))

            axes_label = ttk.Label(frame, text="axes: -", justify="left")
            axes_label.pack(anchor="w")

            buttons_label = ttk.Label(frame, text="buttons pressed: -", justify="left", wraplength=280)
            buttons_label.pack(anchor="w", pady=(6, 0))

            hat_label = ttk.Label(frame, text="hat: (0, 0)")
            hat_label.pack(anchor="w", pady=(6, 0))

            self.debug_widgets[side] = {
                "name": name_label,
                "processed": processed_label,
                "axes": axes_label,
                "buttons": buttons_label,
                "hat": hat_label,
            }

    def _refresh_debug(self):
        snap = self.manager.get_snapshot()
        cfg = self.manager.config
        left, right = snap.get("left"), snap.get("right")
        if left is None or right is None:
            for side in ("left", "right"):
                self.debug_widgets[side]["name"].config(text="Not connected")
            self.after(100, self._refresh_debug)
            return

        vs = compute_virtual_state(left, right, cfg)
        self.schematic.set_active(vs["active_buttons"], vs["lt"], vs["rt"], vs["lx"], vs["ly"], vs["rx"], vs["ry"])

        for side, state, x, y, trig, button_map, hat_map in (
            ("left", left, vs["lx"], vs["ly"], vs["lt"], cfg["left_buttons"], cfg["left_hat"]),
            ("right", right, vs["rx"], vs["ry"], vs["rt"], cfg["right_buttons"], cfg["right_hat"]),
        ):
            widgets = self.debug_widgets[side]
            widgets["name"].config(text=state["name"])
            widgets["processed"].config(text=f"x={x:+.2f} y={y:+.2f}  trigger={bool(trig)}")

            axes_text = "  ".join(f"{i}:{v:+.2f}" for i, v in enumerate(state["axes"]))
            widgets["axes"].config(text=f"axes: {axes_text}")

            trig_target = "LT" if side == "left" else "RT"
            parts = []
            for i, pressed in enumerate(state["buttons"]):
                if not pressed:
                    continue
                if i == cfg["trigger_button"]:
                    parts.append(f"{i}→{trig_target}")
                else:
                    target = button_map.get(str(i))
                    parts.append(f"{i}→{target}" if target else f"{i} (unmapped)")
            widgets["buttons"].config(text="buttons pressed: " + (", ".join(parts) if parts else "none"))

            hat_target = hat_map.get(f"{state['hat'][0]},{state['hat'][1]}")
            hat_text = f"hat: {state['hat']}" + (f" → {hat_target}" if hat_target else "")
            widgets["hat"].config(text=hat_text)

        self.after(50, self._refresh_debug)

    def _recalibrate(self):
        self._connect(recalibrate=True)

    # ------------------------------------------------------------ configs --
    def _build_configs_tab(self):
        tab = self.configs_tab
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))

        self.config_listbox = tk.Listbox(left, exportselection=False, width=22, height=18)
        self.config_listbox.pack(fill="y", expand=True)
        self.config_listbox.bind("<<ListboxSelect>>", lambda e: self._on_config_select())

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="New from Default", command=self._new_config).pack(fill="x")
        ttk.Button(btns, text="Duplicate", command=self._duplicate_config).pack(fill="x", pady=(4, 0))
        self.delete_config_button = ttk.Button(btns, text="Delete", command=self._delete_config)
        self.delete_config_button.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Set as Active", command=self._set_active_from_list).pack(fill="x", pady=(4, 0))

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        self.editor = MappingEditor(right, on_save_as=self._save_as_new_config, on_saved=self._on_config_saved)
        self.editor.pack(fill="both", expand=True)

        self._refresh_config_list(select=config_store.get_last_active_config())

    def _refresh_config_list(self, select=None):
        names = config_store.list_config_names()
        self.config_listbox.delete(0, "end")
        for name in names:
            self.config_listbox.insert("end", name)
        self.active_config_combo.config(values=names)
        target = select if select in names else names[0]
        idx = names.index(target)
        self.config_listbox.selection_clear(0, "end")
        self.config_listbox.selection_set(idx)
        self.config_listbox.see(idx)
        self._on_config_select()

    def _selected_config_name(self):
        sel = self.config_listbox.curselection()
        if not sel:
            return config_store.DEFAULT_CONFIG_NAME
        return self.config_listbox.get(sel[0])

    def _on_config_select(self):
        name = self._selected_config_name()
        config = config_store.load_config(name)
        protected = config_store.is_protected(name)
        self.editor.load(name, config, read_only=protected)
        self.delete_config_button.config(state="disabled" if protected else "normal")

    def _new_config(self):
        name = simpledialog.askstring(APP_TITLE, "Name for the new config (copied from Default):", parent=self)
        if not name:
            return
        try:
            config_store.save_custom_config(name, dict(config_store.DEFAULT_CONFIG))
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._refresh_config_list(select=name)

    def _duplicate_config(self):
        source_name = self._selected_config_name()
        source = config_store.load_config(source_name)
        name = simpledialog.askstring(APP_TITLE, f"Name for the copy of {source_name!r}:", parent=self)
        if not name:
            return
        try:
            config_store.save_custom_config(name, source)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._refresh_config_list(select=name)

    def _delete_config(self):
        name = self._selected_config_name()
        if config_store.is_protected(name):
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete config {name!r}? This can't be undone."):
            return
        config_store.delete_custom_config(name)
        if self.active_config_name.get() == name:
            self.active_config_name.set(config_store.DEFAULT_CONFIG_NAME)
            self._load_active_config(persist=True)
        self._refresh_config_list()

    def _set_active_from_list(self):
        name = self._selected_config_name()
        self.active_config_name.set(name)
        self._load_active_config(persist=True)
        messagebox.showinfo(APP_TITLE, f"{name!r} is now the active mapping.")

    def _on_config_saved(self, name):
        # If the config being edited is also the active one, refresh the
        # manager's copy so the change takes effect immediately.
        if self.active_config_name.get() == name:
            self._load_active_config(persist=False)

    def _save_as_new_config(self, config):
        name = simpledialog.askstring(APP_TITLE, "Save the Default mapping as a new custom config named:", parent=self)
        if not name:
            return
        try:
            config_store.save_custom_config(name, config)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._refresh_config_list(select=name)

    # -------------------------------------------------------------- misc --
    def _load_active_config(self, persist):
        name = self.active_config_name.get()
        try:
            config = config_store.load_config(name)
        except FileNotFoundError:
            name = config_store.DEFAULT_CONFIG_NAME
            self.active_config_name.set(name)
            config = config_store.load_config(name)
        self.manager.set_config(config)
        if persist:
            config_store.set_last_active_config(name)

    def _connect(self, recalibrate):
        with self._connect_lock:
            if getattr(self, "_connecting", False):
                return
            self._connecting = True
        self.recalibrate_button.config(state="disabled")
        self.abort_button.config(state="normal" if recalibrate else "disabled")
        self._abort_calibration.clear()
        self.connection_status.set("Connecting...")
        threading.Thread(target=self._connect_worker, args=(recalibrate,), daemon=True).start()

    def _connect_worker(self, recalibrate):
        try:
            # Stop the poll thread before rediscovering devices: discovery
            # tears down and reinits pygame's joystick subsystem, which
            # would invalidate Joystick objects the poll loop is mid-read on.
            self.manager.stop_polling()
            self.after(0, self._reset_controller_ui)

            sticks, all_joysticks = self.manager.find_sticks()
            if len(sticks) != 2:
                names = ", ".join(j.get_name() for j in all_joysticks) or "none"
                self.after(0, lambda: self._connect_failed(f"Expected 2 T.16000M sticks, found {len(sticks)} ({names})"))
                return

            def status_cb(msg):
                self.after(0, lambda: self.calibration_status.set(msg))

            def progress_cb(remaining):
                self.after(0, lambda: self.calibration_status.set(f"Move the LEFT stick now... ({remaining:0.0f}s)"))

            used_cache = self.manager.assign(
                sticks,
                recalibrate=recalibrate,
                status_cb=status_cb,
                progress_cb=progress_cb,
                should_abort=self._abort_calibration.is_set,
            )
            self.manager.start_polling()
            self.after(0, lambda: self._connect_succeeded(used_cache))
        except Exception as exc:
            self.after(0, lambda: self._connect_failed(str(exc)))

    def _connect_succeeded(self, used_cache):
        self._connecting = False
        left, right = self.manager.left, self.manager.right
        suffix = " (cached assignment)" if used_cache else " (just calibrated)"
        self.connection_status.set(f"Connected -> Left: {left.get_name()}  |  Right: {right.get_name()}{suffix}")
        self.toggle_button.config(state="normal")
        self.recalibrate_button.config(state="normal")
        self.abort_button.config(state="disabled")

    def _connect_failed(self, message):
        self._connecting = False
        self.connection_status.set(f"Not connected ({message})")
        self.toggle_button.config(state="disabled")
        self.recalibrate_button.config(state="normal")
        self.abort_button.config(state="disabled")

    def _on_close(self):
        self._abort_calibration.set()
        try:
            self.manager.stop_polling()
        except Exception:
            pass
        pygame.quit()
        self.destroy()


class MappingEditor(ttk.Frame):
    """Editor for one config: general settings + per-button/hat target pickers."""

    BUTTON_CHOICES = [config_store.UNMAPPED] + sorted(config_store.BUTTON_NAME_MAP.keys())

    def __init__(self, parent, on_save_as, on_saved):
        super().__init__(parent)
        self.on_save_as = on_save_as
        self.on_saved = on_saved
        self.name = None
        self.read_only = True
        self._vars = {}

        header = ttk.Frame(self)
        header.pack(fill="x")
        self.title_label = ttk.Label(header, text="", font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left")
        self.save_button = ttk.Button(header, text="Save", command=self._save)
        self.save_button.pack(side="right")
        self.save_as_button = ttk.Button(header, text="Save As New Custom Config...", command=self._save_as)
        self.save_as_button.pack(side="right", padx=(0, 8))

        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True, pady=(12, 0))
        self.body = scroll.body

        self._build_general_section()
        self._build_mapping_section("left_buttons", "Left stick buttons", config_store.MAPPABLE_BUTTON_INDICES)
        self._build_mapping_section("right_buttons", "Right stick buttons", config_store.MAPPABLE_BUTTON_INDICES)
        self._build_hat_section()

    def _build_general_section(self):
        frame = ttk.LabelFrame(self.body, text="General", padding=10)
        frame.pack(fill="x", padx=4, pady=6)

        def spin(row, key, label, frm, to):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.IntVar()
            ttk.Spinbox(frame, from_=frm, to=to, textvariable=var, width=6).grid(row=row, column=1, sticky="w")
            self._vars[key] = var

        spin(0, "stick_x_axis", "Stick X axis index", 0, 7)
        spin(1, "stick_y_axis", "Stick Y axis index", 0, 7)
        spin(2, "trigger_button", "Trigger button index", 0, 19)
        spin(3, "poll_hz", "Poll rate (Hz)", 30, 250)

        deadzone_var = tk.DoubleVar()
        ttk.Label(frame, text="Stick deadzone").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Scale(frame, from_=0.0, to=0.5, orient="horizontal", variable=deadzone_var, length=160).grid(
            row=4, column=1, sticky="w"
        )
        self._vars["stick_deadzone"] = deadzone_var

        left_invert = tk.BooleanVar()
        right_invert = tk.BooleanVar()
        ttk.Checkbutton(frame, text="Invert left Y", variable=left_invert).grid(row=5, column=0, sticky="w", pady=2)
        ttk.Checkbutton(frame, text="Invert right Y", variable=right_invert).grid(row=5, column=1, sticky="w", pady=2)
        self._vars["invert_y_left"] = left_invert
        self._vars["invert_y_right"] = right_invert

    def _build_mapping_section(self, key, title, indices):
        frame = ttk.LabelFrame(self.body, text=title, padding=10)
        frame.pack(fill="x", padx=4, pady=6)
        per_row = 3
        section_vars = {}
        for row, index in enumerate(indices):
            ttk.Label(frame, text=f"Button {index}").grid(
                row=row // per_row, column=(row % per_row) * 2, sticky="w", padx=(0, 4), pady=2
            )
            var = tk.StringVar(value=config_store.UNMAPPED)
            combo = ttk.Combobox(frame, textvariable=var, values=self.BUTTON_CHOICES, state="readonly", width=13)
            combo.grid(row=row // per_row, column=(row % per_row) * 2 + 1, sticky="w", padx=(0, 12), pady=2)
            section_vars[index] = var
        self._vars[key] = section_vars

    def _build_hat_section(self):
        frame = ttk.LabelFrame(self.body, text="Hat (D-pad)", padding=10)
        frame.pack(fill="x", padx=4, pady=6)
        ttk.Label(frame, text="Direction").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Left stick ->").grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text="Right stick ->").grid(row=0, column=2, sticky="w")

        left_vars, right_vars = {}, {}
        for row, (x, y, label) in enumerate(config_store.HAT_DIRECTIONS, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            lvar = tk.StringVar(value=config_store.UNMAPPED)
            rvar = tk.StringVar(value=config_store.UNMAPPED)
            ttk.Combobox(frame, textvariable=lvar, values=self.BUTTON_CHOICES, state="readonly", width=14).grid(
                row=row, column=1, sticky="w", padx=(0, 12), pady=2
            )
            ttk.Combobox(frame, textvariable=rvar, values=self.BUTTON_CHOICES, state="readonly", width=14).grid(
                row=row, column=2, sticky="w", pady=2
            )
            left_vars[(x, y)] = lvar
            right_vars[(x, y)] = rvar
        self._vars["left_hat"] = left_vars
        self._vars["right_hat"] = right_vars

    def load(self, name, config, read_only):
        self.name = name
        self.read_only = read_only
        self.title_label.config(text=f"{name}{' (read-only)' if read_only else ''}")

        for key in ("stick_x_axis", "stick_y_axis", "trigger_button", "poll_hz", "stick_deadzone"):
            self._vars[key].set(config[key])
        self._vars["invert_y_left"].set(config["invert_y_left"])
        self._vars["invert_y_right"].set(config["invert_y_right"])

        for key in ("left_buttons", "right_buttons"):
            for index, var in self._vars[key].items():
                var.set(config[key].get(str(index), config_store.UNMAPPED))

        for key in ("left_hat", "right_hat"):
            for direction, var in self._vars[key].items():
                dir_str = f"{direction[0]},{direction[1]}"
                var.set(config[key].get(dir_str, config_store.UNMAPPED))

        state = "disabled" if read_only else "readonly"
        self._set_widget_states(state)
        self.save_button.config(state="disabled" if read_only else "normal")
        if read_only:
            self.save_as_button.pack(side="right", padx=(0, 8))
        else:
            self.save_as_button.pack_forget()

    def _set_widget_states(self, combobox_state):
        for widget in self._iter_input_widgets(self.body):
            if isinstance(widget, ttk.Combobox):
                widget.config(state=combobox_state)
            else:
                widget.config(state="disabled" if self.read_only else "normal")

    def _iter_input_widgets(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, (ttk.Combobox, ttk.Spinbox, ttk.Scale, ttk.Checkbutton)):
                yield child
            yield from self._iter_input_widgets(child)

    def _collect_config(self):
        config = {
            "stick_x_axis": self._vars["stick_x_axis"].get(),
            "stick_y_axis": self._vars["stick_y_axis"].get(),
            "trigger_button": self._vars["trigger_button"].get(),
            "poll_hz": self._vars["poll_hz"].get(),
            "stick_deadzone": round(self._vars["stick_deadzone"].get(), 3),
            "invert_y_left": self._vars["invert_y_left"].get(),
            "invert_y_right": self._vars["invert_y_right"].get(),
        }
        for key in ("left_buttons", "right_buttons"):
            config[key] = {
                str(index): var.get() for index, var in self._vars[key].items() if var.get() != config_store.UNMAPPED
            }
        for key in ("left_hat", "right_hat"):
            config[key] = {
                f"{d[0]},{d[1]}": var.get() for d, var in self._vars[key].items() if var.get() != config_store.UNMAPPED
            }
        return config

    def _save(self):
        if self.read_only or self.name is None:
            return
        config = self._collect_config()
        try:
            config_store.save_custom_config(self.name, config)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        messagebox.showinfo(APP_TITLE, f"Saved {self.name!r}.")
        self.on_saved(self.name)

    def _save_as(self):
        self.on_save_as(self._collect_config())


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
