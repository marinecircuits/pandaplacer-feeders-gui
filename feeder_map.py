#!/usr/bin/env python3
"""Bambu feeder map for OpenPnP / PandaPlacer.

Reads an OpenPnP machine.xml and draws a top-down map of where every Bambu
feeder (BambooFeederAutoVision) sits on the machine bed, colour-coded by
whether it is enabled and which part it carries.

Usage:
    python3 feeder_map.py [path/to/machine.xml]

With no argument it reads the live config at ~/.openpnp2/machine.xml.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

BAMBU_CLASS = "org.openpnp.machine.pandaplacer.BambooFeederAutoVision"
DEFAULT_CONFIG = os.path.expanduser("~/.openpnp2/machine.xml")

# Config backups written here (next to this script) before any edit.
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "config_backups")

# Default bed size (mm) used only if the axis soft limits can't be read.
FALLBACK_BED = (318.0, 343.0)

NO_PART = {"", "NC", "None", None}


@dataclass
class Feeder:
    fid: str
    name: str
    enabled: bool
    part: str
    x: float
    y: float
    z: float
    rotation: float
    rotation_in_feeder: str
    feed_count: str

    @property
    def has_part(self) -> bool:
        return self.part not in NO_PART


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
def parse_config(path: str) -> tuple[list[Feeder], tuple[float, float, float, float]]:
    """Return (feeders, (xmin, ymin, xmax, ymax)) parsed from machine.xml."""
    root = ET.parse(path).getroot()

    feeders: list[Feeder] = []
    for f in root.iter("feeder"):
        if BAMBU_CLASS not in f.get("class", ""):
            continue
        loc = f.find("location")
        if loc is None:
            continue
        feeders.append(
            Feeder(
                fid=f.get("id", ""),
                name=f.get("name", "?"),
                enabled=f.get("enabled", "false").lower() == "true",
                part=f.get("part-id", "") or "",
                x=float(loc.get("x", 0.0)),
                y=float(loc.get("y", 0.0)),
                z=float(loc.get("z", 0.0)),
                rotation=float(loc.get("rotation", 0.0)),
                rotation_in_feeder=f.get("rotation-in-feeder", ""),
                feed_count=f.get("feed-count", ""),
            )
        )

    bed = read_bed_extents(root)
    return feeders, bed


def read_bed_extents(root: ET.Element) -> tuple[float, float, float, float]:
    """Read X/Y soft limits to size the bed; fall back to defaults."""
    hi = {"X": FALLBACK_BED[0], "Y": FALLBACK_BED[1]}
    for axis in root.iter("axis"):
        letter = axis.get("letter")
        if letter in hi:
            node = axis.find("soft-limit-high")
            if node is not None and node.get("value"):
                hi[letter] = float(node.get("value"))
    return (0.0, 0.0, hi["X"], hi["Y"])


# --------------------------------------------------------------------------- #
# Editing (backup + surgical feeder removal)
# --------------------------------------------------------------------------- #
def backup_config(path: str) -> str:
    """Copy machine.xml into BACKUP_DIR with a timestamp; return backup path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{ts}.bak")
    shutil.copy2(path, dst)
    return dst


def openpnp_running() -> bool:
    """True if an OpenPnP (java) process is running — editing the config then
    would be pointless, as OpenPnP overwrites machine.xml when it saves/exits.

    Scans /proc (Linux); returns False on other platforms or if unreadable.
    """
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read().decode("utf-8", "ignore")
        except OSError:
            continue
        if not raw:
            continue
        argv = raw.split("\x00")
        # require the executable itself to be java (avoids matching a shell
        # whose arguments merely mention "openpnp")
        if "java" in os.path.basename(argv[0]).lower() \
                and "openpnp" in raw.lower():
            return True
    return False


def remove_feeder_text(text: str, fid: str) -> str:
    """Cut exactly the <feeder id="fid"> ... </feeder> block from raw XML text.

    Feeders are not nested, so the first </feeder> after the matching opening
    tag closes it. Leading indentation and the trailing newline are consumed so
    no blank line is left behind. The result is validated as well-formed XML.
    Raises ValueError if the feeder is not found exactly once.
    """
    pattern = re.compile(
        r'[ \t]*<feeder\b[^>]*\bid="' + re.escape(fid) + r'".*?</feeder>\r?\n?',
        re.DOTALL,
    )
    new_text, n = pattern.subn("", text)
    if n != 1:
        raise ValueError(f"expected 1 feeder with id {fid!r}, matched {n}")
    ET.fromstring(new_text.encode("utf-8"))  # validate; raises on malformed XML
    return new_text


def _feeder_block_re(fid: str) -> re.Pattern:
    return re.compile(
        r'[ \t]*<feeder\b[^>]*\bid="' + re.escape(fid) + r'".*?</feeder>\r?\n?',
        re.DOTALL,
    )


def extract_feeder_block(text: str, fid: str) -> str:
    """Return the raw <feeder> ... </feeder> block (with indent) for a feeder."""
    m = _feeder_block_re(fid).search(text)
    if not m:
        raise ValueError(f"feeder {fid!r} not found")
    return m.group(0)


def update_feeder_text(text: str, fid: str, *, name: str,
                       x: float, y: float, z: float, rotation: float) -> str:
    """Rewrite a feeder's name and pick location in place, leaving the rest of
    its block (pipeline etc.) and the rest of the file untouched."""
    m = _feeder_block_re(fid).search(text)
    if not m:
        raise ValueError(f"feeder {fid!r} not found")
    block = m.group(0)
    bm = re.match(r'(\s*)(<feeder\b[^>]*?>)(.*)', block, re.DOTALL)
    indent, open_tag, rest = bm.groups()
    open_tag = _set_attr(open_tag, "name", name)
    loc = (f'<location units="Millimeters" x="{x}" y="{y}" '
           f'z="{z}" rotation="{rotation}"/>')
    rest, n = re.subn(r'<location\b[^>]*?/>', loc, rest, count=1)
    if n != 1:
        raise ValueError("pick location not found in feeder")
    new_text = text[:m.start()] + indent + open_tag + rest + text[m.end():]
    ET.fromstring(new_text.encode("utf-8"))  # validate
    return new_text


def _rewrite_feeder_open_tag(text: str, fid: str, attrs: dict) -> str:
    """Set one or more attributes on a feeder's opening tag, in place."""
    m = _feeder_block_re(fid).search(text)
    if not m:
        raise ValueError(f"feeder {fid!r} not found")
    block = m.group(0)
    bm = re.match(r'(\s*)(<feeder\b[^>]*?>)(.*)', block, re.DOTALL)
    indent, open_tag, rest = bm.groups()
    for attr, value in attrs.items():
        open_tag = _set_attr(open_tag, attr, value)
    new_text = text[:m.start()] + indent + open_tag + rest + text[m.end():]
    ET.fromstring(new_text.encode("utf-8"))  # validate
    return new_text


def set_feeder_part_text(text: str, fid: str, part: str) -> str:
    """Change only a feeder's part-id attribute, leaving everything else as-is."""
    return _rewrite_feeder_open_tag(text, fid, {"part-id": part})


def set_feeder_enabled_text(text: str, fid: str, enabled: bool) -> str:
    """Change only a feeder's enabled attribute."""
    return _rewrite_feeder_open_tag(text, fid, {"enabled": "true" if enabled
                                                 else "false"})


def make_feeder_id(existing: set[str]) -> str:
    """Generate an OpenPnP-style feeder id ('FDR' + 16 hex) not already in use."""
    import secrets
    while True:
        fid = "FDR" + secrets.token_hex(8)
        if fid not in existing:
            return fid


def _set_attr(tag: str, attr: str, value: str) -> str:
    """Replace attr="..." in an opening tag, matching the attribute exactly.

    The negative lookbehind keeps `name` from matching inside
    `feed-actuator-name`, `id` from matching inside `part-id`, etc.
    """
    pat = re.compile(r'(?<![\w-])' + re.escape(attr) + r'="[^"]*"')
    new_tag, n = pat.subn(f'{attr}="{value}"', tag, count=1)
    if n != 1:
        raise ValueError(f"attribute {attr!r} not found in feeder tag")
    return new_tag


def build_feeder_block(template: str, *, fid: str, name: str, part: str,
                       enabled: bool, x: float, y: float, z: float,
                       rotation: float) -> str:
    """Clone a feeder template block, swapping in the new feeder's fields."""
    m = re.match(r'(\s*)(<feeder\b[^>]*?>)(.*)', template, re.DOTALL)
    if not m:
        raise ValueError("could not parse feeder template")
    indent, open_tag, rest = m.groups()
    open_tag = _set_attr(open_tag, "id", fid)
    open_tag = _set_attr(open_tag, "name", name)
    open_tag = _set_attr(open_tag, "part-id", part)
    open_tag = _set_attr(open_tag, "enabled", "true" if enabled else "false")
    open_tag = _set_attr(open_tag, "feed-count", "0")
    loc = (f'<location units="Millimeters" x="{x}" y="{y}" '
           f'z="{z}" rotation="{rotation}"/>')
    rest, n = re.subn(r'<location\b[^>]*?/>', loc, rest, count=1)
    if n != 1:
        raise ValueError("pick location not found in template")
    return indent + open_tag + rest


def insert_feeder_text(text: str, block: str) -> str:
    """Insert a feeder block just before the </feeders> container close."""
    if not block.endswith("\n"):
        block += "\n"
    new_text, n = re.subn(r'([ \t]*</feeders>)', block + r'\1', text, count=1)
    if n != 1:
        raise ValueError("could not locate </feeders> container")
    ET.fromstring(new_text.encode("utf-8"))  # validate
    return new_text


def read_part_ids(config_path: str) -> list[str]:
    """Read part ids from parts.xml next to machine.xml (empty list if absent)."""
    parts_path = os.path.join(os.path.dirname(config_path), "parts.xml")
    try:
        root = ET.parse(parts_path).getroot()
    except (FileNotFoundError, ET.ParseError):
        return []
    ids = [p.get("id") for p in root.iter("part") if p.get("id")]
    return sorted(ids)


# --------------------------------------------------------------------------- #
# Bank / slot model (preset positions derived from existing feeders)
# --------------------------------------------------------------------------- #
NAME_RE = re.compile(r"PPBF-N(\d+)")
GRID_PITCH = 12.5          # nominal port spacing (mm), used as a fallback
BANK_SPLIT_X = 160.0       # x below this is a left bank, above is a right bank

# Nominal grid for the four standard banks (x, y at port 0, slope per port).
# Used as a fallback when a bank has no feeders left to derive geometry from;
# data-derived values take precedence whenever a bank still has feeders.
DEFAULT_BANKS = {
    0: (11.30, 291.40, -GRID_PITCH),   # left
    1: (11.30, 126.60, -GRID_PITCH),   # left
    2: (307.20, -21.00, GRID_PITCH),   # right
    3: (307.20, 144.00, GRID_PITCH),   # right
}


def slot_name(bank: int, port: int) -> str:
    """Feeder name for a bank/port, zero-padded: (0,5)->'PPBF-N005',
    (2,10)->'PPBF-N210'."""
    return f"PPBF-N{bank * 100 + port:03d}"


def parse_slot(name: str) -> tuple[int, int] | None:
    """Inverse of slot_name: 'PPBF-N210' -> (2, 10); None if it doesn't match."""
    m = NAME_RE.fullmatch(name) or NAME_RE.match(name)
    if not m:
        return None
    num = int(m.group(1))
    return num // 100, num % 100


class BankModel:
    """Per-bank linear fit (x constant, y = y0 + slope*port) plus a clone template.

    Derived from the feeders already present so preset positions track the real
    machine geometry instead of being hard-coded.
    """

    def __init__(self, bank: int, x: float, y0: float, slope: float,
                 z: float, rotation: float, template: Feeder):
        self.bank = bank
        self.x = x
        self.y0 = y0
        self.slope = slope
        self.z = z
        self.rotation = rotation
        self.template = template

    def position(self, port: int) -> tuple[float, float, float, float]:
        return (round(self.x, 3), round(self.y0 + self.slope * port, 3),
                round(self.z, 3), round(self.rotation, 3))


def _fit_sets(feeders: list[Feeder]) -> dict[int, list[tuple[int, Feeder]]]:
    """Group feeders by bank, preferring the uncalibrated disabled templates."""
    groups: dict[int, list[tuple[int, Feeder]]] = {}
    for f in feeders:
        slot = parse_slot(f.name)
        if slot is not None:
            groups.setdefault(slot[0], []).append((slot[1], f))
    return groups


def _estimate_pitch(groups: dict[int, list[tuple[int, Feeder]]]) -> float:
    """Median |Δy/Δport| across consecutive ports = the physical port pitch.

    Using the median makes it immune to the odd off-grid feeder.
    """
    import statistics
    diffs = []
    for items in groups.values():
        pts = sorted(((p, f.y) for p, f in items), key=lambda t: t[0])
        for (p1, y1), (p2, y2) in zip(pts, pts[1:]):
            if p2 != p1:
                diffs.append(abs((y2 - y1) / (p2 - p1)))
    return statistics.median(diffs) if diffs else GRID_PITCH


def derive_bank_models(feeders: list[Feeder]) -> dict[int, BankModel]:
    """Fit a BankModel for each bank found in the feeder names.

    The port pitch is a single value estimated from the whole config; per bank
    only the rail x, a median intercept and the slope sign are taken, so a few
    calibrated/off-grid feeders can't distort the grid.
    """
    import statistics

    groups = _fit_sets(feeders)
    pitch = _estimate_pitch(groups)

    def same_side_template(x: float) -> Feeder | None:
        """A feeder on the same side as x, for cloning into an empty bank."""
        left = x < BANK_SPLIT_X
        cands = [f for f in feeders if (f.x < BANK_SPLIT_X) == left]
        cands = cands or feeders
        return next((f for f in cands if f.enabled), cands[0]) if cands else None

    models: dict[int, BankModel] = {}
    # union of banks seen in the data and the four standard banks, so an
    # emptied bank can still be selected for adding.
    for bank in sorted(set(groups) | set(DEFAULT_BANKS)):
        items = groups.get(bank)
        if items:
            # prefer disabled "template" feeders: they sit on the nominal rail.
            clean = [it for it in items if not it[1].enabled]
            fit_set = clean if clean else items
            x = statistics.median([f.x for _, f in fit_set])
            z = statistics.median([f.z for _, f in items])
            slope = (-1.0 if x < BANK_SPLIT_X else 1.0) * pitch
            y0 = statistics.median([f.y - slope * p for p, f in fit_set])
            # clone: prefer an enabled feeder in the bank, else any in the bank.
            template = next((f for _, f in items if f.enabled), items[0][1])
        elif bank in DEFAULT_BANKS:
            x, y0, slope = DEFAULT_BANKS[bank]
            template = same_side_template(x)
            if template is None:
                continue  # no feeder anywhere to clone a pipeline from
            z = template.z
        else:
            continue
        models[bank] = BankModel(bank, x, y0, slope, z, rotation=0.0,
                                 template=template)
    return models


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class FeederMapApp:
    PAD = 60          # canvas padding (px) around the bed
    MARKER = 9        # feeder marker half-size (px)

    COL_BG = "#1e1f26"
    COL_BED = "#2b2d38"
    COL_BED_EDGE = "#4a4d5e"
    COL_GRID = "#34384a"
    COL_ENABLED = "#3fb950"
    COL_NOPART = "#d29922"
    COL_DISABLED = "#5a5f6e"
    COL_TEXT = "#e6e6e6"
    COL_SEL = "#58a6ff"

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.feeders: list[Feeder] = []
        self.models: dict[int, BankModel] = {}
        self.bed = (0.0, 0.0, *FALLBACK_BED)
        self.items: dict[int, Feeder] = {}     # canvas item id -> feeder
        self.selected: Feeder | None = None

        self.root = tk.Tk()
        self.root.title(f"PandaPlacer — Bambu Feeder Map  [{config_path}]")
        self.root.geometry("1180x820")
        self.root.configure(bg=self.COL_BG)

        self.show_disabled = tk.BooleanVar(value=True)

        self.small = tkfont.Font(family="TkDefaultFont", size=8)
        self.label = tkfont.Font(family="TkDefaultFont", size=10, weight="bold")
        self.mono = tkfont.Font(family="TkFixedFont", size=10)

        self._build_toolbar()
        self._build_body()

        self.load()
        self.canvas.bind("<Configure>", lambda e: self.redraw())

    # -- layout ----------------------------------------------------------- #
    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=self.COL_BG)
        bar.pack(side="top", fill="x", padx=10, pady=8)

        tk.Button(bar, text="⟳ Reload", command=self.load).pack(side="left")
        tk.Button(bar, text="＋ Add feeder",
                  command=self.add_feeder_dialog).pack(side="left", padx=(8, 0))
        self.move_btn = tk.Button(bar, text="⇄ Move selected",
                                  command=self.move_selected, state="disabled")
        self.move_btn.pack(side="left", padx=(8, 0))
        self.part_btn = tk.Button(bar, text="🏷 Set part",
                                  command=self.change_part_selected, state="disabled")
        self.part_btn.pack(side="left", padx=(8, 0))
        self.remove_btn = tk.Button(bar, text="🗑 Remove selected",
                                    command=self.remove_selected, state="disabled")
        self.remove_btn.pack(side="left", padx=(8, 0))
        tk.Checkbutton(
            bar, text="Show disabled", variable=self.show_disabled,
            command=self.redraw, bg=self.COL_BG, fg=self.COL_TEXT,
            selectcolor=self.COL_BED, activebackground=self.COL_BG,
            activeforeground=self.COL_TEXT,
        ).pack(side="left", padx=(12, 0))

        tk.Label(bar, text="Filter:", bg=self.COL_BG, fg=self.COL_TEXT).pack(
            side="left", padx=(16, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.redraw())
        tk.Entry(bar, textvariable=self.filter_var, width=22).pack(side="left")

        self.status = tk.Label(bar, text="", bg=self.COL_BG, fg=self.COL_TEXT)
        self.status.pack(side="right")

        # legend
        leg = tk.Frame(self.root, bg=self.COL_BG)
        leg.pack(side="top", fill="x", padx=10)
        for color, text in (
            (self.COL_ENABLED, "enabled + part"),
            (self.COL_NOPART, "enabled, no part"),
            (self.COL_DISABLED, "disabled"),
        ):
            tk.Label(leg, text="■", fg=color, bg=self.COL_BG,
                     font=("TkDefaultFont", 11)).pack(side="left", padx=(8, 2))
            tk.Label(leg, text=text, bg=self.COL_BG,
                     fg=self.COL_TEXT).pack(side="left")

    def _build_body(self):
        body = tk.Frame(self.root, bg=self.COL_BG)
        body.pack(side="top", fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg=self.COL_BG, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)

        # side detail panel
        side = tk.Frame(body, bg=self.COL_BED, width=300)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="Feeder details", bg=self.COL_BED, fg=self.COL_TEXT,
                 font=("TkDefaultFont", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self.detail = tk.Label(side, text="Hover or click a feeder.",
                               bg=self.COL_BED, fg=self.COL_TEXT, justify="left",
                               anchor="nw", font=self.mono)
        self.detail.pack(fill="both", expand=True, padx=12, pady=4)

        self.tooltip = None

    # -- data ------------------------------------------------------------- #
    def load(self):
        try:
            self.feeders, self.bed = parse_config(self.config_path)
        except FileNotFoundError:
            messagebox.showerror("Not found", f"Config not found:\n{self.config_path}")
            return
        except ET.ParseError as exc:
            messagebox.showerror("Parse error", f"Could not parse XML:\n{exc}")
            return
        self.models = derive_bank_models(self.feeders)
        self.selected = None
        self._set_selection_buttons(False)
        self.redraw()

    def _set_selection_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for attr in ("move_btn", "part_btn", "remove_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.config(state=state)

    # -- coordinate transform --------------------------------------------- #
    def _make_transform(self):
        xmin, ymin, xmax, ymax = self.bed
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        bw, bh = (xmax - xmin) or 1, (ymax - ymin) or 1
        scale = min((cw - 2 * self.PAD) / bw, (ch - 2 * self.PAD) / bh)
        # centre the bed within the canvas
        ox = (cw - bw * scale) / 2
        oy = (ch - bh * scale) / 2

        def to_screen(x: float, y: float) -> tuple[float, float]:
            sx = ox + (x - xmin) * scale
            sy = ch - oy - (y - ymin) * scale   # flip Y so it points up
            return sx, sy

        return to_screen, scale

    # -- drawing ---------------------------------------------------------- #
    def redraw(self):
        c = self.canvas
        c.delete("all")
        self.items.clear()
        if c.winfo_width() < 50:
            return

        to_screen, scale = self._make_transform()
        xmin, ymin, xmax, ymax = self.bed

        # bed
        x0, y0 = to_screen(xmin, ymin)
        x1, y1 = to_screen(xmax, ymax)
        c.create_rectangle(x0, y1, x1, y0, fill=self.COL_BED,
                           outline=self.COL_BED_EDGE, width=2)

        # grid every 50 mm
        step = 50
        gx = xmin - (xmin % step)
        while gx <= xmax:
            sx, _ = to_screen(gx, ymin)
            c.create_line(sx, y0, sx, y1, fill=self.COL_GRID)
            c.create_text(sx, y0 + 12, text=f"{gx:g}", fill=self.COL_GRID,
                          font=self.small)
            gx += step
        gy = ymin - (ymin % step)
        while gy <= ymax:
            _, sy = to_screen(xmin, gy)
            c.create_line(x0, sy, x1, sy, fill=self.COL_GRID)
            c.create_text(x0 - 16, sy, text=f"{gy:g}", fill=self.COL_GRID,
                          font=self.small)
            gy += step

        # origin marker
        ox_s, oy_s = to_screen(0, 0)
        c.create_oval(ox_s - 4, oy_s - 4, ox_s + 4, oy_s + 4,
                      outline=self.COL_SEL, width=2)
        c.create_text(ox_s + 10, oy_s - 10, text="0,0", fill=self.COL_SEL,
                      anchor="w", font=self.small)

        flt = self.filter_var.get().strip().lower()
        shown = 0
        for f in self.feeders:
            if not self.show_disabled.get() and not f.enabled:
                continue
            if flt and flt not in f.name.lower() and flt not in f.part.lower():
                continue
            self._draw_feeder(c, f, to_screen)
            shown += 1

        n_en = sum(1 for f in self.feeders if f.enabled)
        self.status.config(
            text=f"{len(self.feeders)} feeders  ·  {n_en} enabled  ·  {shown} shown"
        )

    def _draw_feeder(self, c, f: Feeder, to_screen):
        sx, sy = to_screen(f.x, f.y)
        m = self.MARKER
        if not f.enabled:
            color = self.COL_DISABLED
        elif f.has_part:
            color = self.COL_ENABLED
        else:
            color = self.COL_NOPART
        outline = self.COL_SEL if f is self.selected else "#11121a"
        width = 3 if f is self.selected else 1

        item = c.create_rectangle(sx - m, sy - m, sx + m, sy + m,
                                  fill=color, outline=outline, width=width)
        self.items[item] = f

        # slot-number label box, placed on the outboard side of the bed so the
        # markers stay in clean columns and labels sit in the margin. The part
        # id (if any) is shown on the same line after the slot number.
        label = f.name.split("-")[-1] if "-" in f.name else f.name
        text = f"{label}  {f.part}" if f.has_part else label
        xmin, _, xmax, _ = self.bed
        on_left = f.x < (xmin + xmax) / 2
        gap = 6
        lw = self.label.measure(text) + 12
        lh = self.label.metrics("linespace") + 6
        if on_left:
            lx1 = sx - m - gap
            lx0 = lx1 - lw
        else:
            lx0 = sx + m + gap
            lx1 = lx0 + lw
        box = c.create_rectangle(lx0, sy - lh / 2, lx1, sy + lh / 2,
                                 fill="#11121a", outline=color, width=width)
        txt = c.create_text((lx0 + lx1) / 2, sy, text=text,
                            fill=self.COL_TEXT, font=self.label)
        # make the label box select the same feeder on hover/click
        self.items[box] = f
        self.items[txt] = f

    # -- interaction ------------------------------------------------------ #
    def _feeder_at(self, x, y) -> Feeder | None:
        for item in self.canvas.find_overlapping(x, y, x, y):
            if item in self.items:
                return self.items[item]
        return None

    def _on_click(self, event):
        f = self._feeder_at(event.x, event.y)
        self.selected = f
        if f:
            self.detail.config(text=self._describe(f))
        self._set_selection_buttons(bool(f))
        self.redraw()

    def _on_double_click(self, event):
        f = self._feeder_at(event.x, event.y)
        if f:
            self.selected = f
            self.toggle_enabled(f)

    def toggle_enabled(self, feeder: Feeder):
        """Flip a feeder's enabled state."""
        if not self._openpnp_guard():
            return
        new_state = not feeder.enabled
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            new_text = set_feeder_enabled_text(text, feeder.fid, new_state)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Toggle failed", str(exc))
            return
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == feeder.fid), None)
        if self.selected:
            self.detail.config(text=self._describe(self.selected))
            self._set_selection_buttons(True)
        self.redraw()
        self.status.config(
            text=f"{'Enabled' if new_state else 'Disabled'} '{feeder.name}'  ·  "
                 f"backup: {os.path.basename(backup)}")

    def _openpnp_guard(self) -> bool:
        """Block edits while OpenPnP is running; return True if safe to proceed."""
        if openpnp_running():
            messagebox.showerror(
                "OpenPnP is running",
                "OpenPnP appears to be running. It overwrites machine.xml when "
                "it saves or exits, so any change made now would be lost.\n\n"
                "Fully quit OpenPnP, then try again. Start OpenPnP only after "
                "editing — it loads the file at startup.")
            return False
        return True

    def remove_selected(self):
        f = self.selected
        if f is None:
            return
        if not self._openpnp_guard():
            return
        part = f.part if f.part not in NO_PART else "no part"
        if not messagebox.askyesno(
            "Remove feeder",
            f"Permanently delete feeder '{f.name}' ({part}) from\n"
            f"{self.config_path}?\n\n"
            "⚠ Close OpenPnP first — if it is running it will overwrite this "
            "change (and may undelete the feeder) when it exits.\n\n"
            "A timestamped backup is written to config_backups/ first.",
        ):
            return
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            new_text = remove_feeder_text(text, f.fid)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Remove failed", str(exc))
            return
        self.selected = None
        self._set_selection_buttons(False)
        self.load()
        self.status.config(
            text=f"Removed '{f.name}'  ·  backup: {os.path.basename(backup)}")

    def move_selected(self):
        if self.selected is None:
            return
        if not self.models:
            messagebox.showerror("No banks", "No bank presets available.")
            return
        MoveFeederDialog(self, self.selected)

    def move_feeder(self, feeder: Feeder, bank: int, port: int):
        """Reposition a feeder onto a bank/port preset and rename it to match."""
        if not self._openpnp_guard():
            return
        model = self.models[bank]
        x, y, z, rot = model.position(port)
        name = slot_name(bank, port)
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            new_text = update_feeder_text(text, feeder.fid, name=name,
                                          x=x, y=y, z=z, rotation=rot)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Move failed", str(exc))
            return
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == feeder.fid), None)
        if self.selected:
            self.detail.config(text=self._describe(self.selected))
            self._set_selection_buttons(True)
        self.redraw()
        self.status.config(
            text=f"Moved to '{name}'  ·  backup: {os.path.basename(backup)}")

    def change_part_selected(self):
        if self.selected is None:
            return
        ChangePartDialog(self, self.selected)

    def change_part(self, feeder: Feeder, part: str):
        """Change a feeder's associated part id."""
        if not self._openpnp_guard():
            return
        part = part.strip() or "NC"
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            new_text = set_feeder_part_text(text, feeder.fid, part)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Set part failed", str(exc))
            return
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == feeder.fid), None)
        if self.selected:
            self.detail.config(text=self._describe(self.selected))
            self._set_selection_buttons(True)
        self.redraw()
        self.status.config(
            text=f"Set part of '{feeder.name}' to '{part}'  ·  "
                 f"backup: {os.path.basename(backup)}")

    def add_feeder_dialog(self):
        if not self.models:
            messagebox.showerror(
                "No banks",
                "Could not derive any feeder banks from the config — need "
                "existing PPBF-N feeders to compute preset positions.")
            return
        AddFeederDialog(self)

    def add_feeder(self, tmpl: Feeder, name: str, part: str, enabled: bool,
                   x: float, y: float, z: float, rotation: float):
        """Build a new feeder from the template block and write it to config."""
        if not self._openpnp_guard():
            return
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            existing = {f.fid for f in self.feeders}
            template_block = extract_feeder_block(text, tmpl.fid)
            new_id = make_feeder_id(existing)
            block = build_feeder_block(
                template_block, fid=new_id, name=name, part=part,
                enabled=enabled, x=x, y=y, z=z, rotation=rotation)
            new_text = insert_feeder_text(text, block)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Add failed", str(exc))
            return
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == new_id), None)
        if self.selected:
            self.detail.config(text=self._describe(self.selected))
            self._set_selection_buttons(True)
        self.redraw()
        messagebox.showinfo(
            "Feeder added",
            f"Added '{name}' (cloned from '{tmpl.name}').\n\n"
            f"Backup saved to:\n{backup}",
        )

    def _on_motion(self, event):
        f = self._feeder_at(event.x, event.y)
        if f and self.selected is None:
            self.detail.config(text=self._describe(f))

    @staticmethod
    def _describe(f: Feeder) -> str:
        part = f.part if f.part not in NO_PART else "— none —"
        return (
            f"Name   : {f.name}\n"
            f"Status : {'ENABLED' if f.enabled else 'disabled'}\n"
            f"Part   : {part}\n"
            f"\n"
            f"X      : {f.x:.2f} mm\n"
            f"Y      : {f.y:.2f} mm\n"
            f"Z      : {f.z:.2f} mm\n"
            f"Rot    : {f.rotation:.2f}°\n"
            f"In-feed: {f.rotation_in_feeder}\n"
            f"Feeds  : {f.feed_count}\n"
            f"\n"
            f"id     : {f.fid}"
        )

    def run(self):
        self.root.mainloop()


class SearchableSelect(tk.Frame):
    """An entry with a live-filtered listbox underneath — type to narrow a long
    list of values. The bound StringVar holds the current value (typed or
    picked), so callers use it exactly like a Combobox's textvariable."""

    def __init__(self, master, app, values, textvariable,
                 width=44, height=8):
        super().__init__(master, bg=app.COL_BG)
        self._all = sorted(values)
        self.var = textvariable

        self.entry = tk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        box = tk.Frame(self, bg=app.COL_BG)
        box.pack(fill="both", expand=True)
        self.lb = tk.Listbox(box, height=height, width=width,
                             bg=app.COL_BED, fg=app.COL_TEXT,
                             selectbackground=app.COL_SEL, highlightthickness=0,
                             activestyle="none", exportselection=False)
        sb = tk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._fill(self._all)
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", self._focus_list)
        self.lb.bind("<<ListboxSelect>>", self._on_pick)
        self.lb.bind("<Double-Button-1>", self._on_pick)
        self.lb.bind("<Return>", self._on_pick)

    def _fill(self, items):
        self.lb.delete(0, "end")
        for it in items:
            self.lb.insert("end", it)

    def _on_key(self, event):
        if event.keysym in ("Up", "Down", "Return", "Escape"):
            return
        typed = self.var.get().strip().lower()
        items = [v for v in self._all if typed in v.lower()] if typed else self._all
        self._fill(items)

    def _focus_list(self, _event):
        if self.lb.size():
            self.lb.focus_set()
            self.lb.selection_clear(0, "end")
            self.lb.selection_set(0)
            self.lb.activate(0)

    def _on_pick(self, _event):
        sel = self.lb.curselection()
        if sel:
            self.var.set(self.lb.get(sel[0]))
            self.entry.focus_set()
            self.entry.icursor("end")


class AddFeederDialog(tk.Toplevel):
    """Add a feeder by picking a bank/port preset; the position is computed
    from the bank model and the vision pipeline is cloned from that bank."""

    PORTS = list(range(13))   # ports 0..12 per bank

    def __init__(self, app: "FeederMapApp"):
        super().__init__(app.root)
        self.app = app
        self.title("Add feeder (preset)")
        self.configure(bg=app.COL_BG)
        self.transient(app.root)
        self.resizable(True, True)
        self.minsize(440, 320)

        pad = {"padx": 10, "pady": 5}
        frm = tk.Frame(self, bg=app.COL_BG)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        def label(r, text):
            tk.Label(frm, text=text, bg=app.COL_BG, fg=app.COL_TEXT).grid(
                row=r, column=0, sticky="e", **pad)

        # Slot = bank
        label(0, "Slot (bank)")
        self.bank_var = tk.StringVar()
        banks = [str(b) for b in sorted(app.models)]
        self.bank_cb = ttk.Combobox(frm, textvariable=self.bank_var,
                                    values=banks, width=10, state="readonly")
        self.bank_cb.grid(row=0, column=1, sticky="w", **pad)

        # Feeder = port
        label(1, "Feeder (port)")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(
            frm, textvariable=self.port_var,
            values=[str(p) for p in self.PORTS], width=10, state="readonly")
        self.port_cb.grid(row=1, column=1, sticky="w", **pad)

        # Part picker — searchable: type to filter the list
        tk.Label(frm, text="Part", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=2, column=0, sticky="ne", **pad)
        self.part_var = tk.StringVar(value="NC")
        parts = read_part_ids(app.config_path)
        part_w = min(60, max(26, *(len(p) for p in parts))) if parts else 26
        SearchableSelect(frm, app, parts, self.part_var, width=part_w).grid(
            row=2, column=1, sticky="we", **pad)

        self.enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(frm, text="Enabled", variable=self.enabled,
                       bg=app.COL_BG, fg=app.COL_TEXT, selectcolor=app.COL_BED,
                       activebackground=app.COL_BG, activeforeground=app.COL_TEXT
                       ).grid(row=3, column=1, sticky="w", **pad)

        # computed-position readout
        self.preview = tk.Label(frm, text="", bg=app.COL_BED, fg=app.COL_TEXT,
                                justify="left", anchor="w", font=app.mono,
                                width=part_w)
        self.preview.grid(row=4, column=0, columnspan=2, sticky="we", **pad)

        btns = tk.Frame(frm, bg=app.COL_BG)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", **pad)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        self.add_btn = tk.Button(btns, text="Add", command=self._submit)
        self.add_btn.pack(side="right", padx=(0, 8))

        self.bank_var.trace_add("write", lambda *_: self._update_preview())
        self.port_var.trace_add("write", lambda *_: self._update_preview())
        self.part_var.trace_add("write", lambda *_: self._update_preview())
        self.bind("<Escape>", lambda e: self.destroy())
        self.bank_var.set(banks[0])
        self.port_var.set("0")
        self.grab_set()

    # current (bank, port) or (None, None) if incomplete
    def _selection(self):
        try:
            return int(self.bank_var.get()), int(self.port_var.get())
        except ValueError:
            return None, None

    def _existing_at(self, bank, port):
        """Existing feeder occupying this bank/port, matched by slot not text
        (so 'N5' is recognised when adding 'N005')."""
        return next((f for f in self.app.feeders
                     if parse_slot(f.name) == (bank, port)), None)

    def _unreachable(self, x, y):
        """True if (x, y) is outside the machine travel (X/Y soft limits)."""
        xmin, ymin, xmax, ymax = self.app.bed
        return not (xmin <= x <= xmax and ymin <= y <= ymax)

    def _update_preview(self):
        bank, port = self._selection()
        if bank is None or bank not in self.app.models:
            self.preview.config(text="select a slot and feeder")
            self.add_btn.config(state="disabled")
            return
        model = self.app.models[bank]
        x, y, z, rot = model.position(port)
        name = slot_name(bank, port)
        warns = []
        if self._unreachable(x, y):
            warns.append("⚠ outside machine travel — NOT reachable by the head.")
        dup = self._existing_at(bank, port)
        if dup:
            warns.append(f"⚠ slot taken by '{dup.name}' — will add a duplicate.")
        warn = ("\n" + "\n".join(warns)) if warns else ""
        self.preview.config(
            text=(f"Name : {name}\n"
                  f"X    : {x:.2f} mm\n"
                  f"Y    : {y:.2f} mm\n"
                  f"Z    : {z:.2f} mm\n"
                  f"Rot  : {rot:.2f}°\n"
                  f"clone: {model.template.name}{warn}"))
        self.add_btn.config(state="normal")

    def _submit(self):
        bank, port = self._selection()
        if bank is None or bank not in self.app.models:
            return
        model = self.app.models[bank]
        name = slot_name(bank, port)
        x, y, z, rot = model.position(port)
        if self._unreachable(x, y) and not messagebox.askyesno(
            "Not reachable",
            f"Slot {bank}/{port} ({name}) is at X={x:.1f} Y={y:.1f}, outside the "
            "machine travel — the head cannot reach it for picking.\n\n"
            "Add it anyway?", parent=self):
            return
        dup = self._existing_at(bank, port)
        if dup and not messagebox.askyesno(
            "Duplicate", f"Slot {bank}/{port} is already taken by '{dup.name}'.\n"
                         "Add another one anyway?", parent=self):
            return
        part = self.part_var.get().strip() or "NC"
        self.destroy()
        self.app.add_feeder(model.template, name, part, self.enabled.get(),
                            x, y, z, rot)


class MoveFeederDialog(tk.Toplevel):
    """Move an existing feeder onto a different bank/port preset."""

    PORTS = list(range(13))

    def __init__(self, app: "FeederMapApp", feeder: Feeder):
        super().__init__(app.root)
        self.app = app
        self.feeder = feeder
        self.title("Move feeder")
        self.configure(bg=app.COL_BG)
        self.transient(app.root)
        self.resizable(False, False)

        pad = {"padx": 10, "pady": 5}
        frm = tk.Frame(self, bg=app.COL_BG)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(frm, text=f"Moving '{feeder.name}'  (part: "
                           f"{feeder.part if feeder.has_part else '—'})",
                 bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)

        tk.Label(frm, text="To slot (bank)", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=1, column=0, sticky="e", **pad)
        self.bank_var = tk.StringVar()
        banks = [str(b) for b in sorted(app.models)]
        ttk.Combobox(frm, textvariable=self.bank_var, values=banks, width=10,
                     state="readonly").grid(row=1, column=1, sticky="w", **pad)

        tk.Label(frm, text="To feeder (port)", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=2, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar()
        ttk.Combobox(frm, textvariable=self.port_var,
                     values=[str(p) for p in self.PORTS], width=10,
                     state="readonly").grid(row=2, column=1, sticky="w", **pad)

        self.preview = tk.Label(frm, text="", bg=app.COL_BED, fg=app.COL_TEXT,
                                justify="left", anchor="w", font=app.mono,
                                width=40)
        self.preview.grid(row=3, column=0, columnspan=2, sticky="we", **pad)

        btns = tk.Frame(frm, bg=app.COL_BG)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", **pad)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        self.move_btn = tk.Button(btns, text="Move", command=self._submit)
        self.move_btn.pack(side="right", padx=(0, 8))

        self.bank_var.trace_add("write", lambda *_: self._update_preview())
        self.port_var.trace_add("write", lambda *_: self._update_preview())
        self.bind("<Escape>", lambda e: self.destroy())
        # default to the feeder's current slot if its name encodes one
        cur = parse_slot(feeder.name)
        self.bank_var.set(str(cur[0]) if cur and cur[0] in app.models else banks[0])
        self.port_var.set(str(cur[1]) if cur else "0")
        self.grab_set()

    def _selection(self):
        try:
            return int(self.bank_var.get()), int(self.port_var.get())
        except ValueError:
            return None, None

    def _unreachable(self, x, y):
        xmin, ymin, xmax, ymax = self.app.bed
        return not (xmin <= x <= xmax and ymin <= y <= ymax)

    def _other_at(self, bank, port):
        """A *different* feeder occupying the target slot, if any."""
        return next((f for f in self.app.feeders
                     if f.fid != self.feeder.fid
                     and parse_slot(f.name) == (bank, port)), None)

    def _update_preview(self):
        bank, port = self._selection()
        if bank is None or bank not in self.app.models:
            self.preview.config(text="select a target slot")
            self.move_btn.config(state="disabled")
            return
        x, y, z, rot = self.app.models[bank].position(port)
        name = slot_name(bank, port)
        warns = []
        if self._unreachable(x, y):
            warns.append("⚠ outside machine travel — NOT reachable by the head.")
        other = self._other_at(bank, port)
        if other:
            warns.append(f"⚠ slot already used by '{other.name}'.")
        warn = ("\n" + "\n".join(warns)) if warns else ""
        self.preview.config(
            text=(f"{self.feeder.name}  →  {name}\n"
                  f"X    : {x:.2f} mm\n"
                  f"Y    : {y:.2f} mm\n"
                  f"Z    : {z:.2f} mm\n"
                  f"Rot  : {rot:.2f}°{warn}"))
        self.move_btn.config(state="normal")

    def _submit(self):
        bank, port = self._selection()
        if bank is None or bank not in self.app.models:
            return
        x, y, z, _ = self.app.models[bank].position(port)
        name = slot_name(bank, port)
        if self._unreachable(x, y) and not messagebox.askyesno(
            "Not reachable",
            f"{name} is at X={x:.1f} Y={y:.1f}, outside the machine travel — "
            "the head cannot reach it.\n\nMove it there anyway?", parent=self):
            return
        other = self._other_at(bank, port)
        if other and not messagebox.askyesno(
            "Slot in use", f"Slot {bank}/{port} is already used by "
                           f"'{other.name}'.\nMove '{self.feeder.name}' there "
                           "anyway?", parent=self):
            return
        self.destroy()
        self.app.move_feeder(self.feeder, bank, port)


class ChangePartDialog(tk.Toplevel):
    """Change the part associated with an existing feeder."""

    def __init__(self, app: "FeederMapApp", feeder: Feeder):
        super().__init__(app.root)
        self.app = app
        self.feeder = feeder
        self.title("Set part")
        self.configure(bg=app.COL_BG)
        self.transient(app.root)
        self.resizable(True, True)
        self.minsize(440, 0)

        pad = {"padx": 10, "pady": 8}
        frm = tk.Frame(self, bg=app.COL_BG)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        tk.Label(frm, text=f"Feeder '{feeder.name}'", bg=app.COL_BG,
                 fg=app.COL_TEXT).grid(row=0, column=0, columnspan=2,
                                       sticky="w", **pad)
        tk.Label(frm, text="Part", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=1, column=0, sticky="ne", **pad)

        parts = read_part_ids(app.config_path)
        part_w = min(60, max(26, *(len(p) for p in parts))) if parts else 26
        self.part_var = tk.StringVar(value=feeder.part if feeder.has_part else "NC")
        sel = SearchableSelect(frm, app, parts, self.part_var, width=part_w)
        sel.grid(row=1, column=1, sticky="we", **pad)
        sel.entry.focus_set()

        btns = tk.Frame(frm, bg=app.COL_BG)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", **pad)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        tk.Button(btns, text="Set", command=self._submit).pack(
            side="right", padx=(0, 8))

        self.bind("<Return>", lambda e: self._submit())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _submit(self):
        part = self.part_var.get()
        self.destroy()
        self.app.change_part(self.feeder, part)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Visualize OpenPnP Bambu feeders.")
    ap.add_argument("config", nargs="?", default=DEFAULT_CONFIG,
                    help=f"path to machine.xml (default: {DEFAULT_CONFIG})")
    args = ap.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"error: config not found: {args.config}", file=sys.stderr)
        return 1

    FeederMapApp(args.config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
