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
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

BAMBU_CLASS = "org.openpnp.machine.pandaplacer.BambooFeederAutoVision"
DEFAULT_CONFIG = os.path.expanduser("~/.openpnp2/machine.xml")

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
        self.bed = (0.0, 0.0, *FALLBACK_BED)
        self.items: dict[int, Feeder] = {}     # canvas item id -> feeder
        self.selected: Feeder | None = None

        self.root = tk.Tk()
        self.root.title("PandaPlacer — Bambu Feeder Map")
        self.root.geometry("1180x820")
        self.root.configure(bg=self.COL_BG)

        self.show_disabled = tk.BooleanVar(value=True)

        self.small = tkfont.Font(family="TkDefaultFont", size=8)
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
        self.selected = None
        self.redraw()

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
        # short label = trailing token of the name (e.g. N205)
        label = f.name.split("-")[-1] if "-" in f.name else f.name
        c.create_text(sx, sy, text=label, fill="#11121a", font=self.small)

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
        self.redraw()

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
