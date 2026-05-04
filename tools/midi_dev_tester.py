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
import tkinter.font as tkfont
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

import mido


DEFAULT_IN_HINT = "S4 MK1 USB2MIDI"
DEFAULT_OUT_HINT = "S4 MK1 USB2MIDI Feedback"

# ---------------------------------------------------------------------------
# ByteMapWindow
# ---------------------------------------------------------------------------
# Shows a live hex grid of the raw USB packet bytes.  Two update paths:
#   • CC-based: reconstructs byte values from incoming MIDI CC messages.
#   • SysEx-based: decodes the raw-USB SysEx packets emitted when the bridge
#     is started with --raw-sysex (format F0 7D 53 34 02 <nibbles> F7).

class ByteMapWindow:
    """Toplevel window: hex grid + bit-breakdown panel for USB packet bytes."""

    COLS = 32
    MAX_BYTES = 512
    _FADE_MS = 2000  # highlight duration in ms

    def __init__(self, master: tk.Tk, base_channel_var: tk.IntVar) -> None:
        self._master = master
        self._base_ch_var = base_channel_var  # 1-based MIDI channel
        self._data = bytearray(self.MAX_BYTES)
        self._changed_at: List[Optional[float]] = [None] * self.MAX_BYTES
        self._selected_idx: Optional[int] = None
        self._labels: List[tk.Label] = []
        self._header_labels: List[tk.Label] = []
        self._addr_labels: List[tk.Label] = []
        self._alive = True
        self._zoom_var = tk.DoubleVar(value=1.0)
        self._base_font_size = 9
        self._grid_canvas: Optional[tk.Canvas] = None
        self._fit_pending = False

        self._win = tk.Toplevel(master)
        self._win.title("Byte Map — USB packet hex view")
        self._win.geometry("1600x900")
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._apply_zoom()
        self._schedule_auto_fit()
        self._tick()

    def _build(self) -> None:
        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar = ttk.Frame(self._win, padding=(6, 4))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="Clear Highlights",
                   command=self._clear_highlights).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(toolbar, text="Source:").pack(side=tk.LEFT)
        self._src_var = tk.StringVar(value="Both")
        ttk.Combobox(toolbar, textvariable=self._src_var,
                     values=["Both", "CC only", "SysEx only"],
                     width=12, state="readonly").pack(side=tk.LEFT, padx=(2, 12))

        ttk.Label(toolbar, text="MIDI base ch:").pack(side=tk.LEFT)
        ttk.Spinbox(toolbar, from_=1, to=16, width=4,
                    textvariable=self._base_ch_var).pack(side=tk.LEFT, padx=(2, 12))

        self._info_var = tk.StringVar(value="Click a cell to inspect bits")
        ttk.Label(toolbar, textvariable=self._info_var,
                  foreground="#888888").pack(side=tk.LEFT)

        # ── Main pane: grid left, bit panel right ─────────────────────────
        paned = ttk.PanedWindow(self._win, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        grid_outer = ttk.Frame(paned)
        paned.add(grid_outer, weight=5)

        self._bit_frame = ttk.LabelFrame(paned, text="Bit Breakdown", padding=8)
        paned.add(self._bit_frame, weight=1)

        self._build_grid(grid_outer)
        self._build_bit_panel()

    # ── Grid ─────────────────────────────────────────────────────────────────

    def _build_grid(self, parent: ttk.Frame) -> None:
        # Column-index header row
        header = tk.Frame(parent, bg="#333333")
        header.pack(fill=tk.X)
        addr_header = tk.Label(header, text="  Addr ", width=7, bg="#333333", fg="#888888",
                               font=("Courier", self._base_font_size))
        addr_header.pack(side=tk.LEFT, padx=1, pady=1)
        self._header_labels.append(addr_header)
        for col in range(self.COLS):
            lbl = tk.Label(header, text=f"{col:02X}", width=3, bg="#333333", fg="#888888",
                           font=("Courier", self._base_font_size))
            lbl.pack(side=tk.LEFT, padx=1, pady=1)
            self._header_labels.append(lbl)

        # Scrollable canvas for the data rows
        canvas = tk.Canvas(parent, highlightthickness=0, bg="#1e1e1e")
        self._grid_canvas = canvas
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg="#1e1e1e")
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1 * e.delta / 120), "units"))
        canvas.bind("<Configure>", self._on_canvas_resize)

        rows = (self.MAX_BYTES + self.COLS - 1) // self.COLS
        for row in range(rows):
            addr = row * self.COLS
            row_frame = tk.Frame(inner, bg="#1e1e1e")
            row_frame.pack(fill=tk.X)
            addr_lbl = tk.Label(row_frame, text=f"0x{addr:03X}", width=7,
                                bg="#1e1e1e", fg="#888888",
                                font=("Courier", self._base_font_size))
            addr_lbl.pack(side=tk.LEFT, padx=1, pady=1)
            self._addr_labels.append(addr_lbl)
            for col in range(self.COLS):
                byte_idx = addr + col
                if byte_idx >= self.MAX_BYTES:
                    break
                lbl = tk.Label(row_frame, text="--", width=3,
                               bg="#2d2d2d", fg="#cccccc",
                               font=("Courier", self._base_font_size), cursor="hand2")
                lbl.pack(side=tk.LEFT, padx=1, pady=1)
                lbl.bind("<Button-1>", lambda e, i=byte_idx: self._select(i))
                self._labels.append(lbl)

    # ── Bit panel ─────────────────────────────────────────────────────────────

    def _build_bit_panel(self) -> None:
        f = self._bit_frame
        self._bit_addr_var = tk.StringVar(value="—")
        self._bit_val_var  = tk.StringVar(value="—")

        ttk.Label(f, text="Byte:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(f, textvariable=self._bit_addr_var,
                  font=("Courier", 10, "bold")).grid(row=0, column=1, columnspan=3, sticky=tk.W)
        ttk.Label(f, text="Value:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Label(f, textvariable=self._bit_val_var,
                  font=("Courier", 10)).grid(row=1, column=1, columnspan=3, sticky=tk.W, pady=(4, 0))

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(
            row=2, column=0, columnspan=4, sticky=tk.EW, pady=6)

        for col, text in enumerate(("Bit", "Val", "CC (byte)", "CC (bit)")):
            ttk.Label(f, text=text, font=("Courier", 9),
                      foreground="#888888").grid(row=3, column=col, sticky=tk.W, padx=(0, 6))

        self._bit_rows: List[Tuple[tk.Label, tk.Label, tk.Label, tk.Label]] = []
        for bit_row, bit in enumerate(range(7, -1, -1)):
            r = 4 + bit_row
            lbl_bit    = tk.Label(f, text=f"b{bit}", font=("Courier", 9), width=3, anchor=tk.W)
            lbl_val    = tk.Label(f, text="-",       font=("Courier", 9, "bold"), width=3, anchor=tk.W)
            lbl_cc_byt = tk.Label(f, text="-",       font=("Courier", 9), width=11, anchor=tk.W)
            lbl_cc_bit = tk.Label(f, text="-",       font=("Courier", 9), width=11, anchor=tk.W)
            lbl_bit.grid(   row=r, column=0, sticky=tk.W)
            lbl_val.grid(   row=r, column=1, sticky=tk.W)
            lbl_cc_byt.grid(row=r, column=2, sticky=tk.W, padx=(0, 4))
            lbl_cc_bit.grid(row=r, column=3, sticky=tk.W)
            self._bit_rows.append((lbl_bit, lbl_val, lbl_cc_byt, lbl_cc_bit))

    # ── Public update API ─────────────────────────────────────────────────────

    def update_byte(self, idx: int, value: int, source: str = "cc") -> None:
        """Update a single byte by its packet index."""
        src_filter = self._src_var.get()
        if src_filter == "CC only"    and source != "cc":     return
        if src_filter == "SysEx only" and source != "sysex":  return
        if idx < 0 or idx >= self.MAX_BYTES:
            return
        self._data[idx] = value & 0xFF
        self._changed_at[idx] = time.monotonic()
        self._refresh_cell(idx)
        if self._selected_idx == idx:
            self._update_bit_panel()

    def update_packet(self, data: bytes, source: str = "sysex") -> None:
        """Update all bytes from a complete raw packet (e.g. decoded SysEx)."""
        for idx, val in enumerate(data[: self.MAX_BYTES]):
            if self._data[idx] != val:
                self.update_byte(idx, val, source)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _cell_bg(self, idx: int) -> str:
        if idx == self._selected_idx:
            return "#1a6b8a"
        changed = self._changed_at[idx]
        if changed is None:
            return "#2d2d2d"
        age = time.monotonic() - changed
        if age < 0.15:  return "#b35900"
        if age < 0.5:   return "#7a3c00"
        if age < 2.0:   return "#3d1e00"
        return "#2d2d2d"

    def _refresh_cell(self, idx: int) -> None:
        if idx >= len(self._labels):
            return
        val = self._data[idx]
        self._labels[idx].configure(text=f"{val:02X}", bg=self._cell_bg(idx))

    def _on_canvas_resize(self, _event) -> None:
        self._schedule_auto_fit()

    def _schedule_auto_fit(self) -> None:
        if self._fit_pending:
            return
        self._fit_pending = True
        self._win.after(50, self._auto_fit_zoom)

    def _auto_fit_zoom(self) -> None:
        self._fit_pending = False
        if not self._alive or self._grid_canvas is None:
            return
        width = self._grid_canvas.winfo_width()
        if width <= 100:
            return

        # Choose the largest font size that fits 1 address column + 32 byte columns.
        best_size = self._base_font_size
        for size in range(7, 22):
            f = tkfont.Font(family="Courier", size=size)
            cell_px = f.measure("000") + 4
            addr_px = f.measure("0x000") + 8
            estimated_total = addr_px + self.COLS * cell_px + 8
            if estimated_total <= width:
                best_size = size
            else:
                break

        self._zoom_var.set(max(0.8, min(2.5, best_size / self._base_font_size)))
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        zoom = max(0.8, min(2.5, float(self._zoom_var.get())))
        font_size = max(7, min(24, int(round(self._base_font_size * zoom))))
        cell_width = 3 if zoom < 1.8 else 2
        addr_width = 7 if zoom < 1.8 else 6

        grid_font = ("Courier", font_size)
        bit_font = ("Courier", max(8, font_size - 1))

        for lbl in self._header_labels:
            if lbl.cget("text").strip() == "Addr":
                lbl.configure(font=grid_font, width=addr_width)
            else:
                lbl.configure(font=grid_font, width=cell_width)
        for lbl in self._addr_labels:
            lbl.configure(font=grid_font, width=addr_width)
        for lbl in self._labels:
            lbl.configure(font=grid_font, width=cell_width)

        for row in self._bit_rows:
            for lbl in row:
                lbl.configure(font=bit_font)

    def _tick(self) -> None:
        if not self._alive:
            return
        now = time.monotonic()
        for idx, changed in enumerate(self._changed_at):
            if changed is not None and (now - changed) < 2.5:
                self._refresh_cell(idx)
        self._win.after(100, self._tick)

    def _select(self, idx: int) -> None:
        old = self._selected_idx
        self._selected_idx = idx
        if old is not None:
            self._refresh_cell(old)
        self._refresh_cell(idx)
        self._update_bit_panel()

    def _update_bit_panel(self) -> None:
        idx = self._selected_idx
        if idx is None:
            return
        val = self._data[idx]
        base_ch = max(0, self._base_ch_var.get() - 1)  # 0-indexed

        self._bit_addr_var.set(f"0x{idx:03X}  (dec {idx})")
        self._bit_val_var.set(f"0x{val:02X}  {val:08b}")

        for bit_row, bit in enumerate(range(7, -1, -1)):
            _, lbl_val, lbl_cc_byt, lbl_cc_bit = self._bit_rows[bit_row]
            bit_on = (val >> bit) & 1
            lbl_val.configure(text=str(bit_on),
                              fg="#4caf50" if bit_on else "#888888")

            # Byte-mode: CC controller = idx % 128, channel offset = idx // 128
            byt_ch = (base_ch + idx // 128) & 0x0F
            byt_cc = idx % 128
            lbl_cc_byt.configure(text=f"ch{byt_ch + 1} cc{byt_cc}")

            # Bit-mode: bit_abs = idx*8 + bit
            bit_abs = idx * 8 + bit
            bit_ch  = (base_ch + bit_abs // 128) & 0x0F
            bit_cc  = bit_abs % 128
            lbl_cc_bit.configure(text=f"ch{bit_ch + 1} cc{bit_cc}")

        self._info_var.set(
            f"Byte 0x{idx:03X} (dec {idx}) | 0x{val:02X} | {val:08b}")

    def _clear_highlights(self) -> None:
        self._changed_at = [None] * self.MAX_BYTES
        for idx in range(self.MAX_BYTES):
            self._refresh_cell(idx)

    def _on_close(self) -> None:
        self._alive = False
        self._win.destroy()

    def is_alive(self) -> bool:
        return self._alive


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

        self._base_channel_var = tk.IntVar(value=1)
        self._byte_map_win: Optional[ByteMapWindow] = None

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
        ttk.Button(log_buttons, text="Byte Map", command=self._open_byte_map).pack(side=tk.LEFT, padx=(6, 0))

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
            self._route_sysex_to_byte_map(msg)
            return

        in_kind, channel, index, value = parsed
        self._insert_log(ts, in_kind, channel, index, value, str(msg))
        self._bump_counter(in_kind, channel, index)
        self._apply_rules(in_kind, channel, index, value)
        if in_kind == "cc":
            self._route_cc_to_byte_map(channel, index, value)

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

    # ── Byte Map helpers ──────────────────────────────────────────────────────

    def _open_byte_map(self) -> None:
        if self._byte_map_win is not None and self._byte_map_win.is_alive():
            self._byte_map_win._win.lift()
            return
        self._byte_map_win = ByteMapWindow(self.root, self._base_channel_var)

    def _route_cc_to_byte_map(self, channel: int, controller: int, value: int) -> None:
        """Reconstruct byte index from a MIDI CC and push to the byte map."""
        win = self._byte_map_win
        if win is None or not win.is_alive():
            return
        base_ch = self._base_channel_var.get()  # 1-based
        ch_offset = (channel - base_ch) & 0x0F  # wraps
        byte_idx = ch_offset * 128 + controller
        # value = (raw_byte >> 1) & 0x7F  ⟹  approx raw_byte = value << 1
        reconstructed = (value & 0x7F) << 1
        win.update_byte(byte_idx, reconstructed, source="cc")

    def _route_sysex_to_byte_map(self, msg: mido.Message) -> None:
        """Decode a raw-USB SysEx packet (F0 7D 53 34 02 <nibbles> F7) and push."""
        win = self._byte_map_win
        if win is None or not win.is_alive():
            return
        if msg.type != "sysex":
            return
        data = msg.data  # mido strips F0/F7; data is a tuple of ints
        # Expected header: 7D 53 34 02
        if len(data) < 5 or data[0] != 0x7D or data[1] != 0x53 or data[2] != 0x34 or data[3] != 0x02:
            return
        nibbles = data[4:]
        if len(nibbles) % 2 != 0:
            return
        raw = bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))
        win.update_packet(raw, source="sysex")


def main() -> None:
    root = tk.Tk()
    app = MidiDevTesterApp(root)
    _ = app
    root.mainloop()


if __name__ == "__main__":
    main()
