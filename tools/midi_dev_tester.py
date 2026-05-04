#!/usr/bin/env python3
"""Simple MIDI development GUI for monitoring and mapping S4 bridge traffic.

Features:
- Live incoming MIDI event log
- Message counters grouped by control key
- Rule-based mapping: incoming key -> outgoing MIDI message
- Connect/disconnect and auto-refresh MIDI port lists

Dependencies:
- mido
- python-rtmidi
- tkinter (bundled with most Python installs)
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

import mido


DEFAULT_IN_HINT = "S4 MK1 USB2MIDI"
DEFAULT_OUT_HINT = "S4 MK1 USB2MIDI Feedback"


@dataclass
class MappingRule:
    in_kind: str  # cc|note_on|note_off
    in_channel: int
    in_index: int  # control or note
    out_kind: str
    out_channel: int
    out_index: int
    value_mode: str  # passthrough|fixed
    fixed_value: int
    enabled: bool

    def incoming_key(self) -> Tuple[str, int, int]:
        return (self.in_kind, self.in_channel, self.in_index)


class MidiEngine:
    def __init__(self) -> None:
        self._in_port = None
        self._out_port = None
        self._lock = threading.Lock()

    @staticmethod
    def list_inputs() -> List[str]:
        return mido.get_input_names()

    @staticmethod
    def list_outputs() -> List[str]:
        return mido.get_output_names()

    def connected(self) -> bool:
        with self._lock:
            return self._in_port is not None and self._out_port is not None

    def _disconnect_locked(self) -> None:
        if self._in_port is not None:
            self._in_port.close()
            self._in_port = None
        if self._out_port is not None:
            self._out_port.close()
            self._out_port = None

    def connect(self, in_name: str, out_name: str, callback) -> None:
        with self._lock:
            self._disconnect_locked()
            self._out_port = mido.open_output(out_name)
            try:
                self._in_port = mido.open_input(in_name, callback=callback)
            except Exception:
                # Roll back partially-open output port if input open fails.
                self._disconnect_locked()
                raise

    def disconnect(self) -> None:
        with self._lock:
            self._disconnect_locked()

    def send(self, msg: mido.Message) -> None:
        with self._lock:
            if self._out_port is None:
                return
            self._out_port.send(msg)


class MidiDevTesterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("S4 MIDI Dev Tester")
        self.root.geometry("1200x760")

        self.engine = MidiEngine()
        self.event_queue: queue.Queue = queue.Queue()

        self.rules: List[MappingRule] = []
        self.rule_by_key: Dict[Tuple[str, int, int], List[MappingRule]] = {}
        self.counts: Dict[Tuple[str, int, int], int] = {}

        self._build_ui()
        self._refresh_ports()
        self._load_demo_rules()

        self.root.after(25, self._drain_event_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Input Port:").grid(row=0, column=0, sticky=tk.W)
        self.in_var = tk.StringVar()
        self.in_combo = ttk.Combobox(top, textvariable=self.in_var, width=55, state="readonly")
        self.in_combo.grid(row=0, column=1, padx=6, sticky=tk.W)

        ttk.Label(top, text="Output Port:").grid(row=0, column=2, sticky=tk.W)
        self.out_var = tk.StringVar()
        self.out_combo = ttk.Combobox(top, textvariable=self.out_var, width=55, state="readonly")
        self.out_combo.grid(row=0, column=3, padx=6, sticky=tk.W)

        ttk.Button(top, text="Refresh", command=self._refresh_ports).grid(row=0, column=4, padx=6)
        self.connect_btn = ttk.Button(top, text="Connect", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=5, padx=6)

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=6, padx=8, sticky=tk.W)

        center = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        center.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = ttk.Frame(center)
        center.add(left, weight=3)

        right = ttk.Frame(center)
        center.add(right, weight=2)

        # Incoming log
        ttk.Label(left, text="Incoming MIDI Events").pack(anchor=tk.W)
        self.log_tree = ttk.Treeview(
            left,
            columns=("time", "kind", "chan", "index", "value", "raw"),
            show="headings",
            height=22,
        )
        for key, width in (
            ("time", 100),
            ("kind", 90),
            ("chan", 60),
            ("index", 70),
            ("value", 70),
            ("raw", 420),
        ):
            self.log_tree.heading(key, text=key.upper())
            self.log_tree.column(key, width=width, anchor=tk.W)
        self.log_tree.pack(fill=tk.BOTH, expand=True)

        log_buttons = ttk.Frame(left)
        log_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(log_buttons, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

        # Counters
        ttk.Label(left, text="Incoming Counters (by key)").pack(anchor=tk.W, pady=(10, 0))
        self.counter_tree = ttk.Treeview(
            left,
            columns=("kind", "chan", "index", "count"),
            show="headings",
            height=8,
        )
        for key, width in (("kind", 90), ("chan", 60), ("index", 70), ("count", 90)):
            self.counter_tree.heading(key, text=key.upper())
            self.counter_tree.column(key, width=width, anchor=tk.W)
        self.counter_tree.pack(fill=tk.BOTH, expand=False)

        # Mapping panel
        ttk.Label(right, text="Mapping Rules (incoming -> outgoing)").pack(anchor=tk.W)
        self.rules_tree = ttk.Treeview(
            right,
            columns=(
                "enabled",
                "in_kind",
                "in_ch",
                "in_idx",
                "out_kind",
                "out_ch",
                "out_idx",
                "mode",
                "fixed",
            ),
            show="headings",
            height=18,
        )
        headings = (
            ("enabled", "ON", 40),
            ("in_kind", "IN_KIND", 85),
            ("in_ch", "IN_CH", 60),
            ("in_idx", "IN_IDX", 70),
            ("out_kind", "OUT_KIND", 95),
            ("out_ch", "OUT_CH", 70),
            ("out_idx", "OUT_IDX", 80),
            ("mode", "VAL_MODE", 80),
            ("fixed", "FIXED", 60),
        )
        for key, title, width in headings:
            self.rules_tree.heading(key, text=title)
            self.rules_tree.column(key, width=width, anchor=tk.W)
        self.rules_tree.pack(fill=tk.BOTH, expand=True)

        editor = ttk.LabelFrame(right, text="Rule Editor", padding=8)
        editor.pack(fill=tk.X, pady=(8, 0))

        self.in_kind_var = tk.StringVar(value="cc")
        self.in_ch_var = tk.StringVar(value="1")
        self.in_idx_var = tk.StringVar(value="0")
        self.out_kind_var = tk.StringVar(value="cc")
        self.out_ch_var = tk.StringVar(value="1")
        self.out_idx_var = tk.StringVar(value="0")
        self.mode_var = tk.StringVar(value="passthrough")
        self.fixed_var = tk.StringVar(value="127")
        self.enabled_var = tk.BooleanVar(value=True)

        ttk.Label(editor, text="In kind").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(editor, textvariable=self.in_kind_var, values=["cc", "note_on", "note_off"], width=10, state="readonly").grid(row=0, column=1, padx=4)
        ttk.Label(editor, text="In ch").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(editor, textvariable=self.in_ch_var, width=5).grid(row=0, column=3, padx=4)
        ttk.Label(editor, text="In idx").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(editor, textvariable=self.in_idx_var, width=6).grid(row=0, column=5, padx=4)

        ttk.Label(editor, text="Out kind").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Combobox(editor, textvariable=self.out_kind_var, values=["cc", "note_on", "note_off"], width=10, state="readonly").grid(row=1, column=1, padx=4, pady=(6, 0))
        ttk.Label(editor, text="Out ch").grid(row=1, column=2, sticky=tk.W, pady=(6, 0))
        ttk.Entry(editor, textvariable=self.out_ch_var, width=5).grid(row=1, column=3, padx=4, pady=(6, 0))
        ttk.Label(editor, text="Out idx").grid(row=1, column=4, sticky=tk.W, pady=(6, 0))
        ttk.Entry(editor, textvariable=self.out_idx_var, width=6).grid(row=1, column=5, padx=4, pady=(6, 0))

        ttk.Label(editor, text="Value").grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Combobox(editor, textvariable=self.mode_var, values=["passthrough", "fixed"], width=10, state="readonly").grid(row=2, column=1, padx=4, pady=(6, 0))
        ttk.Label(editor, text="Fixed").grid(row=2, column=2, sticky=tk.W, pady=(6, 0))
        ttk.Entry(editor, textvariable=self.fixed_var, width=6).grid(row=2, column=3, padx=4, pady=(6, 0))
        ttk.Checkbutton(editor, text="Enabled", variable=self.enabled_var).grid(row=2, column=4, columnspan=2, sticky=tk.W, pady=(6, 0))

        buttons = ttk.Frame(editor)
        buttons.grid(row=3, column=0, columnspan=6, sticky=tk.W, pady=(10, 0))
        ttk.Button(buttons, text="Add Rule", command=self._add_rule_from_editor).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Delete Selected", command=self._delete_selected_rule).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Duplicate Selected", command=self._duplicate_selected_rule).pack(side=tk.LEFT)

        manual = ttk.LabelFrame(right, text="Manual Outgoing Test", padding=8)
        manual.pack(fill=tk.X, pady=(8, 0))
        self.manual_kind_var = tk.StringVar(value="cc")
        self.manual_ch_var = tk.StringVar(value="1")
        self.manual_idx_var = tk.StringVar(value="0")
        self.manual_val_var = tk.StringVar(value="127")

        ttk.Combobox(manual, textvariable=self.manual_kind_var, values=["cc", "note_on", "note_off"], width=10, state="readonly").grid(row=0, column=0, padx=4)
        ttk.Entry(manual, textvariable=self.manual_ch_var, width=5).grid(row=0, column=1, padx=4)
        ttk.Entry(manual, textvariable=self.manual_idx_var, width=6).grid(row=0, column=2, padx=4)
        ttk.Entry(manual, textvariable=self.manual_val_var, width=6).grid(row=0, column=3, padx=4)
        ttk.Button(manual, text="Send", command=self._send_manual).grid(row=0, column=4, padx=6)

    def _load_demo_rules(self) -> None:
        # A few starter mappings that are easy to tweak in place.
        self.rules = [
            MappingRule("cc", 1, 0, "cc", 1, 0, "passthrough", 127, True),
            MappingRule("note_on", 1, 36, "note_on", 1, 36, "passthrough", 127, True),
        ]
        self._rebuild_rule_index()
        self._refresh_rules_tree()

    def _refresh_ports(self) -> None:
        inputs = self.engine.list_inputs()
        outputs = self.engine.list_outputs()
        self.in_combo["values"] = inputs
        self.out_combo["values"] = outputs

        if inputs and not self.in_var.get():
            preferred_in = self._pick_default_port(inputs, DEFAULT_IN_HINT)
            self.in_var.set(preferred_in)
        if outputs and not self.out_var.get():
            preferred_out = self._pick_default_port(outputs, DEFAULT_OUT_HINT)
            self.out_var.set(preferred_out)

    @staticmethod
    def _pick_default_port(names: List[str], hint: str) -> str:
        for name in names:
            if hint.lower() in name.lower():
                return name
        return names[0]

    def _toggle_connection(self) -> None:
        if self.engine.connected():
            self.engine.disconnect()
            self.status_var.set("Disconnected")
            self.connect_btn.configure(text="Connect")
            return

        in_name = self.in_var.get().strip()
        out_name = self.out_var.get().strip()
        if not in_name or not out_name:
            messagebox.showerror("MIDI", "Select both input and output ports.")
            return

        try:
            self.engine.connect(in_name, out_name, self._midi_callback)
        except Exception as exc:
            messagebox.showerror("MIDI", f"Failed to connect: {exc}")
            return

        self.status_var.set(f"Connected: IN={in_name}  OUT={out_name}")
        self.connect_btn.configure(text="Disconnect")

    def _midi_callback(self, msg: mido.Message) -> None:
        self.event_queue.put(("midi", msg, time.time()))

    def _drain_event_queue(self) -> None:
        for _ in range(200):
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(item)
        self.root.after(25, self._drain_event_queue)

    def _handle_event(self, item) -> None:
        kind = item[0]
        if kind != "midi":
            return

        msg: mido.Message = item[1]
        ts: float = item[2]

        parsed = self._parse_msg(msg)
        if parsed is None:
            self._insert_log(ts, msg.type, "-", "-", "-", str(msg))
            return

        in_kind, channel, index, value = parsed
        self._insert_log(ts, in_kind, channel, index, value, str(msg))
        self._bump_counter(in_kind, channel, index)
        self._apply_rules(in_kind, channel, index, value)

    @staticmethod
    def _parse_msg(msg: mido.Message) -> Optional[Tuple[str, int, int, int]]:
        if msg.type == "control_change":
            return ("cc", msg.channel + 1, msg.control, msg.value)
        if msg.type == "note_on":
            return ("note_on", msg.channel + 1, msg.note, msg.velocity)
        if msg.type == "note_off":
            return ("note_off", msg.channel + 1, msg.note, msg.velocity)
        return None

    def _insert_log(self, ts: float, kind: str, channel, index, value, raw: str) -> None:
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        self.log_tree.insert("", 0, values=(stamp, kind, channel, index, value, raw))
        children = self.log_tree.get_children()
        if len(children) > 1200:
            for row in children[1000:]:
                self.log_tree.delete(row)

    def _bump_counter(self, in_kind: str, ch: int, idx: int) -> None:
        key = (in_kind, ch, idx)
        self.counts[key] = self.counts.get(key, 0) + 1

        row_id = f"{in_kind}:{ch}:{idx}"
        if self.counter_tree.exists(row_id):
            self.counter_tree.item(row_id, values=(in_kind, ch, idx, self.counts[key]))
            return
        self.counter_tree.insert("", "end", iid=row_id, values=(in_kind, ch, idx, self.counts[key]))

    def _apply_rules(self, in_kind: str, ch: int, idx: int, in_value: int) -> None:
        matches = self.rule_by_key.get((in_kind, ch, idx), [])
        for rule in matches:
            if not rule.enabled:
                continue
            out_value = in_value if rule.value_mode == "passthrough" else rule.fixed_value
            out_value = max(0, min(127, out_value))
            msg = self._build_out_msg(rule.out_kind, rule.out_channel, rule.out_index, out_value)
            if msg is not None:
                self.engine.send(msg)

    @staticmethod
    def _build_out_msg(kind: str, channel: int, index: int, value: int) -> Optional[mido.Message]:
        channel0 = channel - 1
        if channel0 < 0 or channel0 > 15:
            return None
        index = max(0, min(127, index))
        value = max(0, min(127, value))

        if kind == "cc":
            return mido.Message("control_change", channel=channel0, control=index, value=value)
        if kind == "note_on":
            return mido.Message("note_on", channel=channel0, note=index, velocity=value)
        if kind == "note_off":
            return mido.Message("note_off", channel=channel0, note=index, velocity=value)
        return None

    def _clear_log(self) -> None:
        for item in self.log_tree.get_children():
            self.log_tree.delete(item)

    def _add_rule_from_editor(self) -> None:
        try:
            rule = MappingRule(
                in_kind=self.in_kind_var.get().strip(),
                in_channel=self._read_int(self.in_ch_var.get(), 1, 16, "In channel"),
                in_index=self._read_int(self.in_idx_var.get(), 0, 127, "In index"),
                out_kind=self.out_kind_var.get().strip(),
                out_channel=self._read_int(self.out_ch_var.get(), 1, 16, "Out channel"),
                out_index=self._read_int(self.out_idx_var.get(), 0, 127, "Out index"),
                value_mode=self.mode_var.get().strip(),
                fixed_value=self._read_int(self.fixed_var.get(), 0, 127, "Fixed value"),
                enabled=bool(self.enabled_var.get()),
            )
        except ValueError as exc:
            messagebox.showerror("Rule", str(exc))
            return

        self.rules.append(rule)
        self._rebuild_rule_index()
        self._refresh_rules_tree()

    def _delete_selected_rule(self) -> None:
        selected = self.rules_tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        if idx < 0 or idx >= len(self.rules):
            return
        del self.rules[idx]
        self._rebuild_rule_index()
        self._refresh_rules_tree()

    def _duplicate_selected_rule(self) -> None:
        selected = self.rules_tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        if idx < 0 or idx >= len(self.rules):
            return
        r = self.rules[idx]
        self.rules.append(
            MappingRule(
                r.in_kind,
                r.in_channel,
                r.in_index,
                r.out_kind,
                r.out_channel,
                r.out_index,
                r.value_mode,
                r.fixed_value,
                r.enabled,
            )
        )
        self._rebuild_rule_index()
        self._refresh_rules_tree()

    def _refresh_rules_tree(self) -> None:
        for item in self.rules_tree.get_children():
            self.rules_tree.delete(item)
        for idx, r in enumerate(self.rules):
            self.rules_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    "Y" if r.enabled else "N",
                    r.in_kind,
                    r.in_channel,
                    r.in_index,
                    r.out_kind,
                    r.out_channel,
                    r.out_index,
                    r.value_mode,
                    r.fixed_value,
                ),
            )

    def _rebuild_rule_index(self) -> None:
        self.rule_by_key = {}
        for rule in self.rules:
            key = rule.incoming_key()
            self.rule_by_key.setdefault(key, []).append(rule)

    def _send_manual(self) -> None:
        try:
            kind = self.manual_kind_var.get().strip()
            ch = self._read_int(self.manual_ch_var.get(), 1, 16, "Manual channel")
            idx = self._read_int(self.manual_idx_var.get(), 0, 127, "Manual index")
            val = self._read_int(self.manual_val_var.get(), 0, 127, "Manual value")
        except ValueError as exc:
            messagebox.showerror("Manual", str(exc))
            return

        msg = self._build_out_msg(kind, ch, idx, val)
        if msg is None:
            messagebox.showerror("Manual", "Invalid outgoing message")
            return
        self.engine.send(msg)

    @staticmethod
    def _read_int(text: str, lo: int, hi: int, name: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{name} must be an integer")
        if value < lo or value > hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return value

    def _on_close(self) -> None:
        self.engine.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = MidiDevTesterApp(root)
    _ = app
    root.mainloop()


if __name__ == "__main__":
    main()
