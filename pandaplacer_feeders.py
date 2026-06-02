#!/usr/bin/env python3
"""PandaPlacer Bamboo feeders — view and edit OpenPnP feeders.

Reads an OpenPnP machine.xml and draws a top-down map of where every Bamboo
feeder (BambooFeederAutoVision) sits on the machine bed, colour-coded by
whether it is enabled and which part it carries, and lets you add, move,
remove, re-part, enable/disable and set the tape rotation/advance of feeders.
It can also actuate a feeder over serial (via pyserial) to perform a feed.

Usage:
    python3 pandaplacer_feeders.py [path/to/machine.xml]

With no argument it reads the live config at ~/.openpnp2/machine.xml.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

BAMBOO_CLASS = "org.openpnp.machine.pandaplacer.BambooFeederAutoVision"
DEFAULT_CONFIG = os.path.expanduser("~/.openpnp2/machine.xml")

# Config backups written here (next to this script) before any edit.
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "config_backups")

# Name of the shared PandaPlacer per-user config folder, created inside the OS
# config dir. It is intentionally general — the feeder map's orientation map is
# just one of the files that may live here alongside other PandaPlacer configs.
APP_DIR_NAME = "pandaplacer-feeders-gui"


def default_config_dir() -> str:
    """OS-appropriate per-user config directory for PandaPlacer data.

    Linux/other Unix : $XDG_CONFIG_HOME/pandaplacer-feeders-gui  (~/.config/…)
    macOS            : ~/Library/Application Support/pandaplacer-feeders-gui
    Windows          : %APPDATA%\\pandaplacer-feeders-gui
    """
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(
            r"~\AppData\Roaming")
    else:                                   # Linux / other Unix
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser(
            "~/.config")
    return os.path.join(base, APP_DIR_NAME)


def ensure_config_dir() -> str:
    """Create the per-user config dir if needed and return its path."""
    path = default_config_dir()
    os.makedirs(path, exist_ok=True)
    return path


# Preferred tape orientations are stored in a simple JSON map inside the shared
# PandaPlacer config dir: { "<part-id>": "<rotation-in-feeder>", ... }. It is
# consulted when adding a feeder and updated whenever a tape rotation is
# applied, so a part's orientation is remembered across configs even after the
# feeder is removed.
ORIENTATIONS_FILE = os.path.join(default_config_dir(), "tape_orientations.json")

# Preferred tape advances are stored the same way, in a sibling JSON map:
# { "<part-id>": <advance-mm>, ... }. Like the orientation map it is consulted
# when adding a feeder and updated whenever a tape advance is applied, so a
# part's tape pitch is remembered across configs.
ADVANCES_FILE = os.path.join(default_config_dir(), "tape_advances.json")

# General app settings (serial port, baud, …) live in a small JSON file in the
# same per-user config folder.
SETTINGS_FILE = os.path.join(default_config_dir(), "settings.json")
DEFAULT_BAUD = 19200

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
    post_pick_actuator: str
    move_before_feed: bool

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
        if BAMBOO_CLASS not in f.get("class", ""):
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
                post_pick_actuator=f.get("post-pick-actuator-name", ""),
                move_before_feed=f.get("move-before-feed", "false").lower()
                == "true",
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
                       x: float, y: float, z: float, rotation: float,
                       actuator_value: str | None = None) -> str:
    """Rewrite a feeder's name and pick location in place, leaving the rest of
    its block (pipeline etc.) and the rest of the file untouched.

    If actuator_value is given, the feed-actuator-value and
    post-pick-actuator-value are rewritten too, so a feeder moved to a new slot
    drives that slot's auto-feeder instead of its old one."""
    m = _feeder_block_re(fid).search(text)
    if not m:
        raise ValueError(f"feeder {fid!r} not found")
    block = m.group(0)
    bm = re.match(r'(\s*)(<feeder\b[^>]*?>)(.*)', block, re.DOTALL)
    indent, open_tag, rest = bm.groups()
    open_tag = _set_attr(open_tag, "name", name)
    if actuator_value is not None:
        open_tag = _set_or_add_attr(open_tag, "feed-actuator-value",
                                    str(actuator_value))
        open_tag = _set_or_add_attr(open_tag, "post-pick-actuator-value",
                                    str(actuator_value))
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


def set_feeder_rotation_in_feeder_text(text: str, fid: str,
                                       rotation: float) -> str:
    """Change only a feeder's rotation-in-feeder attribute (the part's
    orientation in the tape, separate from the pick location's rotation)."""
    return _rewrite_feeder_open_tag(
        text, fid, {"rotation-in-feeder": f"{rotation}"})


def set_feeder_post_pick_actuator_text(text: str, fid: str,
                                       actuator_name: str) -> str:
    """Change only a feeder's post-pick-actuator-name — the tape advance, e.g.
    'AutoFeeder_4mmAdvance'. The post-pick value (slot number) is left as-is."""
    return _rewrite_feeder_open_tag(
        text, fid, {"post-pick-actuator-name": actuator_name})


def set_feeder_move_before_feed_text(text: str, fid: str,
                                     enabled: bool) -> str:
    """Change only a feeder's move-before-feed attribute."""
    return _rewrite_feeder_open_tag(
        text, fid, {"move-before-feed": "true" if enabled else "false"})


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


def _set_or_add_attr(tag: str, attr: str, value: str) -> str:
    """Like _set_attr, but append the attribute if it isn't already present.

    Used for attributes a template might lack (e.g. rotation-in-feeder on a
    feeder that never had its tape orientation set).
    """
    pat = re.compile(r'(?<![\w-])' + re.escape(attr) + r'="[^"]*"')
    new_tag, n = pat.subn(f'{attr}="{value}"', tag, count=1)
    if n == 1:
        return new_tag
    m = re.search(r'\s*/?>\s*$', tag)        # closing of the open tag
    if not m:
        raise ValueError("malformed feeder open tag")
    return tag[:m.start()] + f' {attr}="{value}"' + tag[m.start():]


def build_feeder_block(template: str, *, fid: str, name: str, part: str,
                       enabled: bool, x: float, y: float, z: float,
                       rotation: float,
                       rotation_in_feeder: str | None = None,
                       actuator_value: str | None = None,
                       post_pick_actuator: str | None = None) -> str:
    """Clone a feeder template block, swapping in the new feeder's fields.

    If rotation_in_feeder is given it overrides the template's tape orientation
    (added if the template lacks the attribute); otherwise the clone keeps the
    template's value.

    If post_pick_actuator is given it overrides the template's tape advance
    (post-pick-actuator-name, e.g. 'AutoFeeder_4mmAdvance'); otherwise the
    clone keeps the template's.

    If actuator_value is given it overrides both feed-actuator-value and
    post-pick-actuator-value (the auto-feeder's slot number); otherwise the
    clone keeps the template's — which would be the *template's* slot, not the
    new feeder's, so callers cloning into a different slot should pass it.
    """
    m = re.match(r'(\s*)(<feeder\b[^>]*?>)(.*)', template, re.DOTALL)
    if not m:
        raise ValueError("could not parse feeder template")
    indent, open_tag, rest = m.groups()
    open_tag = _set_attr(open_tag, "id", fid)
    open_tag = _set_attr(open_tag, "name", name)
    open_tag = _set_attr(open_tag, "part-id", part)
    open_tag = _set_attr(open_tag, "enabled", "true" if enabled else "false")
    open_tag = _set_attr(open_tag, "feed-count", "0")
    if rotation_in_feeder is not None:
        open_tag = _set_or_add_attr(open_tag, "rotation-in-feeder",
                                    str(rotation_in_feeder))
    if actuator_value is not None:
        open_tag = _set_or_add_attr(open_tag, "feed-actuator-value",
                                    str(actuator_value))
        open_tag = _set_or_add_attr(open_tag, "post-pick-actuator-value",
                                    str(actuator_value))
    if post_pick_actuator is not None:
        open_tag = _set_or_add_attr(open_tag, "post-pick-actuator-name",
                                    post_pick_actuator)
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
# Tape-rotation memory + standard R/C orientation
# --------------------------------------------------------------------------- #
# 0603/0805 resistors (R) and capacitors (C) ship in tape rotated 90° CW
# relative to their upright CAD footprint, so a new feeder for one defaults to
# this unless a previous configuration remembered something else.
RC_0603_0805_RE = re.compile(r"[RC]_?0(603|805)", re.IGNORECASE)
RC_DEFAULT_ROTATION = "-90"


def default_rotation_for_part(part: str) -> str | None:
    """The standard tape rotation for a part, or None if it has no default.

    Currently only 0603/0805 resistors and capacitors (part ids containing
    R0603/C0603/R0805/C0805) have one: -90°. Inductors (L_0603) and other
    0603-sized parts (0603_LED) are deliberately excluded.
    """
    if part and part not in NO_PART and RC_0603_0805_RE.search(part):
        return RC_DEFAULT_ROTATION
    return None


def load_orientations(path: str = ORIENTATIONS_FILE) -> dict[str, str]:
    """Read the part -> tape-rotation map (empty dict if missing/invalid)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_orientation(part: str, rotation: str,
                     path: str = ORIENTATIONS_FILE) -> None:
    """Remember `part`'s tape rotation in the JSON map (create/update the file).

    No-op for parts with no real part id. Existing entries are preserved and the
    file is written sorted, for a stable, human-editable, diffable result.
    """
    if part in NO_PART:
        return
    data = load_orientations(path)
    if data.get(part) == str(rotation):
        return                          # already up to date — avoid a needless write
    data[part] = str(rotation)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(data.items())), fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def resolve_tape_rotation(part: str,
                          orientations: dict[str, str]) -> tuple[str | None, str | None]:
    """Decide the tape rotation for a new feeder carrying `part`.

    Returns (rotation, source):
      * a value remembered in the orientations map (source 'history'),
      * else the standard 0603/0805 R/C default (source 'default'),
      * else (None, None) — keep whatever the cloned template has.

    A remembered orientation is preferred over the R/C default.
    """
    if part in NO_PART:
        return None, None
    if part in orientations:
        return orientations[part], "history"
    default = default_rotation_for_part(part)
    if default is not None:
        return default, "default"
    return None, None


# --------------------------------------------------------------------------- #
# Tape-advance (post-pick actuator) memory
# --------------------------------------------------------------------------- #
# The tape advance — how far the tape steps after a pick — is encoded in the
# post-pick-actuator-name as 'AutoFeeder_<N>mmAdvance' (the machine defines a
# 2/4/8/12 mm set). It is a property of the part's tape, so it is remembered
# per part just like the orientation.
ADVANCE_RE = re.compile(r"AutoFeeder_(\d+)mmAdvance")
ADVANCE_ACTUATOR_FMT = "AutoFeeder_{}mmAdvance"
DEFAULT_ADVANCES_MM = (2, 4, 8, 12)


def advance_mm_from_actuator(name: str) -> int | None:
    """The advance distance in mm from a post-pick actuator name, or None.

    'AutoFeeder_4mmAdvance' -> 4; anything that isn't an advance actuator
    (e.g. '' or 'AutoFeeder_PostPick') -> None.
    """
    if not name:
        return None
    m = ADVANCE_RE.fullmatch(name) or ADVANCE_RE.search(name)
    return int(m.group(1)) if m else None


def actuator_for_advance_mm(mm: int) -> str:
    """The post-pick actuator name for an advance distance: 4 -> 'AutoFeeder_4mmAdvance'."""
    return ADVANCE_ACTUATOR_FMT.format(int(mm))


def read_advance_actuators(config_path: str) -> list[int]:
    """Tape-advance distances (mm) the machine defines, sorted ascending.

    Parsed from the AutoFeeder_<N>mmAdvance actuators in machine.xml; falls
    back to the standard 2/4/8/12 mm set if none are found or it can't be read.
    """
    try:
        root = ET.parse(config_path).getroot()
    except (FileNotFoundError, ET.ParseError):
        return list(DEFAULT_ADVANCES_MM)
    found = {advance_mm_from_actuator(a.get("name", ""))
             for a in root.iter("actuator")}
    found.discard(None)
    return sorted(found) if found else list(DEFAULT_ADVANCES_MM)


def load_advances(path: str = ADVANCES_FILE) -> dict[str, int]:
    """Read the part -> tape-advance-mm map (empty dict if missing/invalid)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def save_advance(part: str, mm: int, path: str = ADVANCES_FILE) -> None:
    """Remember `part`'s tape advance (mm) in the JSON map (create/update it).

    No-op for parts with no real part id. Existing entries are preserved and
    the file is written sorted, for a stable, human-editable, diffable result.
    """
    if part in NO_PART:
        return
    data = load_advances(path)
    if data.get(part) == int(mm):
        return                          # already up to date — avoid a needless write
    data[part] = int(mm)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(data.items())), fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def resolve_advance(part: str,
                    advances: dict[str, int]) -> tuple[int | None, str | None]:
    """Decide the tape advance (mm) for a new feeder carrying `part`.

    Returns (advance_mm, source): a value remembered in the advances map
    (source 'history'), else (None, None) — keep whatever the cloned template
    has. There is no size-based default for advance (unlike R/C rotation).
    """
    if part in NO_PART:
        return None, None
    if part in advances:
        return advances[part], "history"
    return None, None


# --------------------------------------------------------------------------- #
# General settings + serial auto-feeder actuation
# --------------------------------------------------------------------------- #
# Actuating a feeder talks to the PPBFC AS feeder controller over a serial
# port. The protocol mirrors the PPBFC AS Feeder Test Tool: each port is
# addressed by its slot number N (= bank*100 + port, the same number used for
# the feeder name and actuator value), and a feed is a short g-code sequence.
# pyserial is imported lazily so the rest of the app still runs without it.


def load_settings(path: str = SETTINGS_FILE) -> dict:
    """Read general app settings (empty dict if missing/invalid)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict, path: str = SETTINGS_FILE) -> None:
    """Write general app settings, creating the config dir if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def list_serial_ports() -> list[str]:
    """Available serial port device names (empty list if pyserial is absent)."""
    try:
        import serial.tools.list_ports as list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports()]


def feed_command_sequence(slot_n: int, advance_mm: int) -> list[str]:
    """The g-code lines that feed one auto-feeder port once.

    Mirrors the PPBFC AS Feeder Test Tool's Activate + Single Advance: enable
    the controller, deactivate all ports, activate port N, advance F mm, then
    deactivate port N again.
    """
    return [
        "M610 S1",                              # enable controller
        "M611 S0",                              # deactivate all ports
        f"M611 N{slot_n} S1",                   # activate this port
        f"M600 N{slot_n} F{advance_mm} X1",     # advance F mm
        f"M611 N{slot_n} S0",                   # deactivate this port
    ]


def perform_feed(port: str, baud: int, slot_n: int, advance_mm: int, *,
                 log=None, settle_s: float = 0.3, delay_s: float = 0.2,
                 sleep_fn=time.sleep) -> None:
    """Open `port`, send the feed sequence for `slot_n`, then close it.

    Raises ImportError if pyserial is missing, or serial.SerialException (and
    other OSError subclasses) if the port can't be opened/written. `log(text)`,
    if given, receives one entry per line sent.
    """
    try:
        import serial
    except ImportError as exc:                  # pragma: no cover - env-specific
        raise ImportError(
            "pyserial is required to actuate feeders — install it with "
            "'pip install pyserial'.") from exc
    emit = log or (lambda _text: None)
    ser = serial.Serial(port, baud, timeout=5)
    try:
        sleep_fn(settle_s)                      # let the controller come up
        for cmd in feed_command_sequence(slot_n, advance_mm):
            ser.write((cmd + "\n").encode("utf-8"))
            ser.flush()
            emit(cmd)
            sleep_fn(delay_s)
    finally:
        ser.close()


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


def slot_label(name: str) -> str:
    """Short map label: the slot number zero-padded to 3 digits.

    'PPBF-N0' -> 'N000', 'PPBF-N11' -> 'N011', 'PPBF-N312-Reserved' -> 'N312'.
    Falls back to the trailing name segment for names without a slot number.
    """
    slot = parse_slot(name)
    if slot is not None:
        bank, port = slot
        return f"N{bank * 100 + port:03d}"
    return name.split("-")[-1] if "-" in name else name


def actuator_value_for_name(name: str) -> str | None:
    """The Bamboo auto-feeder actuator value for a slot feeder.

    Each PPBF-N<num> feeder drives the auto-feeder identified by its slot
    number (bank*100 + port = the number in the name), so both
    feed-actuator-value and post-pick-actuator-value equal it: PPBF-N305 ->
    "305.0", PPBF-N7 -> "7.0". Returns None for a name that doesn't encode a
    slot, in which case the cloned template's value is kept untouched.

    The actuator *names* (e.g. AutoFeeder_4mm/8mm/12mmAdvance) depend on the
    tape width, not the slot, so they are left as cloned.
    """
    slot = parse_slot(name)
    if slot is None:
        return None
    bank, port = slot
    return f"{bank * 100 + port}.0"


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
def grab_when_visible(win: tk.Toplevel) -> None:
    """Make `win` modal once it is actually mapped.

    Calling grab_set() straight from a Toplevel's __init__ races the window
    manager: the window often isn't viewable yet, raising
    'grab failed: window not viewable'. Retry on the event loop until it is
    viewable (or the window is gone)."""
    try:
        if not win.winfo_exists():
            return
        win.grab_set()
    except tk.TclError:
        win.after(20, lambda: grab_when_visible(win))


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
    COL_KEY = "#9aa0ad"     # muted label colour for detail-panel keys
    COL_SEL = "#58a6ff"

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.feeders: list[Feeder] = []
        self.models: dict[int, BankModel] = {}
        self.orientations: dict[str, str] = {}
        self.advances: dict[str, int] = {}
        self.settings: dict = load_settings()
        self.bed = (0.0, 0.0, *FALLBACK_BED)
        self.items: dict[int, Feeder] = {}     # canvas item id -> feeder
        self.selected: Feeder | None = None
        self._detail_for: object = object()    # feeder currently in the panel
        self._feeding = False                  # a serial feed is in progress
        self._feed_confirmed = False           # feed warning shown once/session

        self.root = tk.Tk()
        self.root.title(f"PandaPlacer — Bamboo Feeders  [{config_path}]")
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
        # Per-property editing now lives in the double-click Edit feeder dialog;
        # the toolbar keeps only what that dialog doesn't cover.
        self.feed_btn = tk.Button(bar, text="▶ Perform feed",
                                  command=self.perform_feed_selected,
                                  state="disabled")
        self.feed_btn.pack(side="left", padx=(8, 0))
        self.remove_btn = tk.Button(bar, text="🗑 Remove selected",
                                    command=self.remove_selected, state="disabled")
        self.remove_btn.pack(side="left", padx=(8, 0))
        tk.Button(bar, text="⚙ Settings",
                  command=self.open_settings_dialog).pack(side="left",
                                                          padx=(8, 0))
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
        # Two-column key/value grid — the grid geometry manager keeps the value
        # column aligned regardless of font, so it no longer relies on a
        # monospaced font and manual space-padding to line up.
        self.detail = tk.Frame(side, bg=self.COL_BED)
        self.detail.pack(fill="both", expand=True, padx=12, pady=4)
        self.detail.columnconfigure(1, weight=1)
        self._show_details(None)

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
        self.orientations = load_orientations()
        self.advances = load_advances()
        self.selected = None
        self._show_details(None)
        self._set_selection_buttons(False)
        self.redraw()

    def _set_selection_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.remove_btn.config(state=state)
        # don't re-enable the feed button mid-feed
        if not self._feeding:
            self.feed_btn.config(state=state)

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
        # markers stay in clean columns and labels sit in the margin. The slot
        # number is kept next to the marker (right end of the box for left-side
        # feeders, left end for right-side ones) so the numbers line up against
        # the bed; the part id (if any) sits on the other side.
        label = slot_label(f.name)
        xmin, _, xmax, _ = self.bed
        on_left = f.x < (xmin + xmax) / 2
        if not f.has_part:
            text = label
        elif on_left:
            text = f"{f.part}  {label}"
        else:
            text = f"{label}  {f.part}"
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
        self._show_details(f)
        self._set_selection_buttons(bool(f))
        self.redraw()

    def _on_double_click(self, event):
        f = self._feeder_at(event.x, event.y)
        if f:
            self.selected = f
            self._show_details(f)
            self._set_selection_buttons(True)
            self.redraw()
            EditFeederDialog(self, f)

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

    def open_settings_dialog(self):
        SettingsDialog(self)

    def perform_feed_selected(self):
        """Actuate the selected feeder over serial using its configured advance."""
        f = self.selected
        if f is None or self._feeding:
            return
        advance_mm = advance_mm_from_actuator(f.post_pick_actuator)
        if advance_mm is None:
            messagebox.showerror(
                "No tape advance",
                f"'{f.name}' has no tape advance set (post-pick-actuator-name "
                "isn't an AutoFeeder_<N>mmAdvance). Set it via double-click → "
                "Edit feeder first.")
            return

        def on_start():
            self.feed_btn.config(state="disabled", text="▶ Feeding…")
            self.status.config(text=f"Feeding '{f.name}'…")

        def on_progress(cmd):
            self.status.config(text=f"Feeding '{f.name}'…  → {cmd}")

        def on_done(name, slot_n, mm, err):
            self.feed_btn.config(
                text="▶ Perform feed",
                state="normal" if self.selected else "disabled")
            if err is None:
                self.status.config(
                    text=f"Fed '{name}'  ·  N{slot_n}  ·  {mm} mm")
            else:
                messagebox.showerror("Feed failed", str(err))
                self.status.config(text=f"Feed failed: {err}")

        self.feed_feeder(f, advance_mm, on_start=on_start,
                         on_progress=on_progress, on_done=on_done)

    def feed_feeder(self, feeder, advance_mm, *, parent=None, on_start=None,
                    on_progress=None, on_done=None) -> bool:
        """Validate, confirm (once per session) and run a serial feed of
        `advance_mm` for `feeder` on a background thread.

        Shared by the toolbar Perform feed button and the Edit dialog's manual
        feed buttons. The optional callbacks all run on the main thread:
        on_start(), on_progress(cmd), on_done(name, slot_n, advance_mm, err).
        Returns True if the feed was started, False if blocked or declined.
        """
        if feeder is None or self._feeding:
            return False
        parent = parent or self.root
        port = self.settings.get("serial_port")
        if not port:
            messagebox.showerror(
                "No serial port",
                "No serial port is configured. Open ⚙ Settings and choose the "
                "feeder controller's port first.", parent=parent)
            return False
        baud = int(self.settings.get("baud", DEFAULT_BAUD))
        slot = parse_slot(feeder.name)
        if slot is None:
            messagebox.showerror(
                "Unknown slot",
                f"Can't determine a feeder slot (port N) from the name "
                f"'{feeder.name}', so the feed command can't be addressed.",
                parent=parent)
            return False
        slot_n = slot[0] * 100 + slot[1]
        # Warn before the first physical feed; once confirmed, don't ask again
        # for the rest of the session.
        if not self._feed_confirmed:
            if not messagebox.askyesno(
                "Perform feed",
                f"Feed '{feeder.name}' now?\n\n"
                f"Port N : {slot_n}\n"
                f"Advance: {advance_mm} mm\n"
                f"Serial : {port} @ {baud} baud\n\n"
                "⚠ The feeder will physically advance the tape.\n"
                "(Shown only once per session — later feeds run immediately.)",
                parent=parent):
                return False
            self._feed_confirmed = True
        self._feeding = True
        if on_start:
            on_start()

        # Run serial I/O off the GUI thread. The worker only mutates `result`
        # (never Tk); the main thread polls it via after(), so every widget
        # update happens on the main thread.
        result: dict = {}

        def worker():
            try:
                perform_feed(port, baud, slot_n, advance_mm,
                             log=lambda c: result.__setitem__("last", c))
                result["err"] = None
            except Exception as exc:        # noqa: BLE001 - surface any failure
                result["err"] = exc
            result["done"] = True

        threading.Thread(target=worker, daemon=True).start()
        self._poll_feed(result, feeder.name, slot_n, advance_mm,
                        on_progress, on_done)
        return True

    def _poll_feed(self, result, name, slot_n, advance_mm, on_progress,
                   on_done):
        if not result.get("done"):
            if on_progress and result.get("last"):
                on_progress(result["last"])
            self.root.after(80, self._poll_feed, result, name, slot_n,
                            advance_mm, on_progress, on_done)
            return
        self._feeding = False
        if on_done:
            on_done(name, slot_n, advance_mm, result.get("err"))

    def edit_feeder(self, feeder: Feeder, *, reposition: bool, bank: int,
                    port: int, part: str, enabled: bool,
                    rotation_in_feeder: float, advance_mm: int,
                    move_before_feed: bool):
        """Apply every editable property of a feeder in one backup + write.

        Position is only rewritten when `reposition` is set (the slot was
        changed), so a calibrated feeder left on its slot keeps its exact x/y.
        The remembered tape rotation / advance for the part are updated too.
        """
        if not self._openpnp_guard():
            return
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            if reposition:
                model = self.models[bank]
                x, y, z, rot = model.position(port)
                name = slot_name(bank, port)
                text = update_feeder_text(
                    text, feeder.fid, name=name, x=x, y=y, z=z, rotation=rot,
                    actuator_value=actuator_value_for_name(name))
            else:
                name = feeder.name
            text = set_feeder_part_text(text, feeder.fid, part)
            text = set_feeder_enabled_text(text, feeder.fid, enabled)
            text = set_feeder_rotation_in_feeder_text(
                text, feeder.fid, rotation_in_feeder)
            text = set_feeder_post_pick_actuator_text(
                text, feeder.fid, actuator_for_advance_mm(advance_mm))
            text = set_feeder_move_before_feed_text(
                text, feeder.fid, move_before_feed)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Edit failed", str(exc))
            return
        save_orientation(part, rotation_in_feeder)   # remember preferences
        save_advance(part, advance_mm)
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == feeder.fid), None)
        if self.selected:
            self._show_details(self.selected)
            self._set_selection_buttons(True)
        self.redraw()
        self.status.config(
            text=f"Edited '{name}'  ·  backup: {os.path.basename(backup)}")

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
        """Build a new feeder from the template block and write it to config.

        The tape orientation (rotation-in-feeder) is resolved from the part:
        a value remembered in the orientations map is preferred, else the
        standard -90° for 0603/0805 R/C parts, else the template's own value.
        The applied orientation is then saved back to the map.

        The auto-feeder actuator value (feed/post-pick) is set from the new
        feeder's slot number so it drives the right physical feeder, rather
        than inheriting the cloned template's slot.

        The tape advance (post-pick-actuator-name) is set from the part's
        remembered value if there is one, else the cloned template's is kept.
        """
        if not self._openpnp_guard():
            return
        tape_rot, src = resolve_tape_rotation(part, self.orientations)
        actuator_value = actuator_value_for_name(name)
        adv_mm, adv_src = resolve_advance(part, self.advances)
        post_pick = actuator_for_advance_mm(adv_mm) if adv_mm is not None else None
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                text = fh.read()
            existing = {f.fid for f in self.feeders}
            template_block = extract_feeder_block(text, tmpl.fid)
            new_id = make_feeder_id(existing)
            block = build_feeder_block(
                template_block, fid=new_id, name=name, part=part,
                enabled=enabled, x=x, y=y, z=z, rotation=rotation,
                rotation_in_feeder=tape_rot, actuator_value=actuator_value,
                post_pick_actuator=post_pick)
            new_text = insert_feeder_text(text, block)
            backup = backup_config(self.config_path)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Add failed", str(exc))
            return
        if tape_rot is not None:
            save_orientation(part, tape_rot)   # remember for next time
        if adv_mm is not None:
            save_advance(part, adv_mm)         # remember for next time
        self.load()
        self.selected = next((f for f in self.feeders if f.fid == new_id), None)
        if self.selected:
            self._show_details(self.selected)
            self._set_selection_buttons(True)
        self.redraw()
        if src == "history":
            tape_note = (f"Tape rotation set to {tape_rot}° — remembered for "
                         f"'{part}' from {os.path.basename(ORIENTATIONS_FILE)}.\n\n")
        elif src == "default":
            tape_note = (f"Tape rotation set to {tape_rot}° — standard "
                         f"orientation for 0603/0805 resistors & capacitors "
                         f"(saved for next time).\n\n")
        else:
            tape_note = ""
        adv_note = (f"Tape advance set to {adv_mm} mm — remembered for "
                    f"'{part}'.\n\n") if adv_src == "history" else ""
        messagebox.showinfo(
            "Feeder added",
            f"Added '{name}' (cloned from '{tmpl.name}').\n\n"
            f"{tape_note}"
            f"{adv_note}"
            f"Backup saved to:\n{backup}",
        )

    def _on_motion(self, event):
        f = self._feeder_at(event.x, event.y)
        if f and self.selected is None:
            self._show_details(f)

    @staticmethod
    def _detail_rows(f: Feeder) -> list[tuple[str, str] | None]:
        """The (key, value) rows for the detail panel; None = a group spacer."""
        part = f.part if f.part not in NO_PART else "— none —"
        rot_in = f.rotation_in_feeder.strip()
        tape_rot = f"{rot_in}°" if rot_in else "—"
        adv_mm = advance_mm_from_actuator(f.post_pick_actuator)
        advance = f"{adv_mm} mm" if adv_mm is not None else \
            (f.post_pick_actuator or "—")
        return [
            ("Name", f.name),
            ("Status", "ENABLED" if f.enabled else "disabled"),
            ("Part", part),
            None,
            ("X", f"{f.x:.2f} mm"),
            ("Y", f"{f.y:.2f} mm"),
            ("Z", f"{f.z:.2f} mm"),
            ("Rotation in tape", tape_rot),
            None,
            ("Advance", advance),
            ("Move feed", "yes" if f.move_before_feed else "no"),
            ("Feeds", f.feed_count or "0"),
            None,
            ("id", f.fid),
        ]

    def _show_details(self, f: Feeder | None):
        """Render a feeder (or the empty-state hint) into the detail grid.

        Skips the rebuild when the same feeder is already shown, so hovering
        doesn't thrash the widget tree on every mouse move."""
        if f is self._detail_for:
            return
        self._detail_for = f
        for w in self.detail.winfo_children():
            w.destroy()
        if f is None:
            tk.Label(self.detail, text="Hover or click a feeder.",
                     bg=self.COL_BED, fg=self.COL_TEXT, anchor="w",
                     justify="left").grid(row=0, column=0, columnspan=2,
                                          sticky="w")
            return
        keyfont = ("TkDefaultFont", 9)
        for r, item in enumerate(self._detail_rows(f)):
            if item is None:
                tk.Frame(self.detail, bg=self.COL_BED, height=8).grid(row=r,
                                                                      column=0)
                continue
            key, val = item
            tk.Label(self.detail, text=key, bg=self.COL_BED, fg=self.COL_KEY,
                     anchor="w", font=keyfont).grid(row=r, column=0, sticky="w",
                                                    padx=(0, 12), pady=1)
            tk.Label(self.detail, text=val, bg=self.COL_BED, fg=self.COL_TEXT,
                     anchor="w", justify="left", font=self.mono,
                     wraplength=190).grid(row=r, column=1, sticky="w", pady=1)

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
        grab_when_visible(self)

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
        x, y, z, _ = model.position(port)
        name = slot_name(bank, port)
        warns = []
        if self._unreachable(x, y):
            warns.append("⚠ outside machine travel — NOT reachable by the head.")
        dup = self._existing_at(bank, port)
        if dup:
            warns.append(f"⚠ slot taken by '{dup.name}' — will add a duplicate.")
        part = self.part_var.get().strip() or "NC"
        tape_rot, src = resolve_tape_rotation(part, self.app.orientations)
        if src == "history":
            tape_line = f"Tape : {tape_rot}°  (remembered)"
        elif src == "default":
            tape_line = f"Tape : {tape_rot}°  (auto: 0603/0805 R/C)"
        else:
            tape_line = "Tape : (template default)"
        adv_mm, adv_src = resolve_advance(part, self.app.advances)
        if adv_src == "history":
            adv_line = f"Adv  : {adv_mm} mm  (remembered)"
        else:
            adv_line = "Adv  : (template default)"
        warn = ("\n" + "\n".join(warns)) if warns else ""
        act = actuator_value_for_name(name)
        self.preview.config(
            text=(f"Name : {name}\n"
                  f"X    : {x:.2f} mm\n"
                  f"Y    : {y:.2f} mm\n"
                  f"Z    : {z:.2f} mm\n"
                  f"{tape_line}\n"
                  f"{adv_line}\n"
                  f"Act  : {act}  (feed + post-pick)\n"
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


class EditFeederDialog(tk.Toplevel):
    """Edit every property of a feeder in one dialog (opened by double-click).

    Slot, part, enabled, tape rotation, tape advance and move-before-feed are
    all editable, and applied in a single backup + write. The position is only
    rewritten if the slot (bank/port) is actually changed, so a feeder left on
    its slot keeps its exact (possibly calibrated) x/y.
    """

    PORTS = list(range(13))
    ROT_PRESETS = ("-90", "0", "90", "180", "270")

    def __init__(self, app: "FeederMapApp", feeder: Feeder):
        super().__init__(app.root)
        self.app = app
        self.feeder = feeder
        self.title(f"Edit feeder — {feeder.name}")
        self.configure(bg=app.COL_BG)
        self.transient(app.root)
        self.resizable(True, True)
        self.minsize(460, 0)

        pad = {"padx": 10, "pady": 5}
        frm = tk.Frame(self, bg=app.COL_BG)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        def label(r, text, sticky="e"):
            tk.Label(frm, text=text, bg=app.COL_BG, fg=app.COL_TEXT).grid(
                row=r, column=0, sticky=sticky, **pad)

        tk.Label(frm, text=f"id  {feeder.fid}", bg=app.COL_BG, fg=app.COL_KEY,
                 font=app.small).grid(row=0, column=0, columnspan=2,
                                      sticky="w", **pad)

        # Slot (bank / port) — repositions only if changed
        cur = parse_slot(feeder.name)
        banks = [str(b) for b in sorted(app.models)]
        self.has_slots = bool(banks)
        label(1, "Slot (bank / port)")
        slot_row = tk.Frame(frm, bg=app.COL_BG)
        slot_row.grid(row=1, column=1, sticky="w", **pad)
        if self.has_slots:
            self.bank_var = tk.StringVar(
                value=str(cur[0]) if cur and cur[0] in app.models else banks[0])
            self.port_var = tk.StringVar(value=str(cur[1]) if cur else "0")
            ttk.Combobox(slot_row, textvariable=self.bank_var, values=banks,
                         width=6, state="readonly").pack(side="left")
            tk.Label(slot_row, text=" / ", bg=app.COL_BG,
                     fg=app.COL_TEXT).pack(side="left")
            ttk.Combobox(slot_row, textvariable=self.port_var,
                         values=[str(p) for p in self.PORTS], width=6,
                         state="readonly").pack(side="left")
            self._init_slot = (self.bank_var.get(), self.port_var.get())
            self.bank_var.trace_add("write", lambda *_: self._update_preview())
            self.port_var.trace_add("write", lambda *_: self._update_preview())
        else:
            self.bank_var = self.port_var = None
            self._init_slot = (None, None)
            tk.Label(slot_row, text="(no bank presets — position not editable)",
                     bg=app.COL_BG, fg=app.COL_KEY).pack(side="left")

        # Part
        tk.Label(frm, text="Part", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=2, column=0, sticky="ne", **pad)
        parts = read_part_ids(app.config_path)
        part_w = min(60, max(26, *(len(p) for p in parts))) if parts else 26
        self.part_var = tk.StringVar(
            value=feeder.part if feeder.has_part else "NC")
        SearchableSelect(frm, app, parts, self.part_var, width=part_w,
                         height=6).grid(row=2, column=1, sticky="we", **pad)

        # Enabled
        self.enabled_var = tk.BooleanVar(value=feeder.enabled)
        tk.Checkbutton(frm, text="Enabled", variable=self.enabled_var,
                       bg=app.COL_BG, fg=app.COL_TEXT, selectcolor=app.COL_BED,
                       activebackground=app.COL_BG, activeforeground=app.COL_TEXT
                       ).grid(row=3, column=1, sticky="w", **pad)

        # Rotation in tape
        label(4, "Rotation in tape (°)")
        rot_row = tk.Frame(frm, bg=app.COL_BG)
        rot_row.grid(row=4, column=1, sticky="w", **pad)
        self.rot_var = tk.StringVar(value=feeder.rotation_in_feeder or "0")
        ttk.Combobox(rot_row, textvariable=self.rot_var,
                     values=self.ROT_PRESETS, width=10).pack(side="left")
        tk.Button(rot_row, text="?", width=2,
                  command=lambda: RotationHelpDialog(app, self)).pack(
                      side="left", padx=(6, 0))

        # Tape advance
        label(5, "Tape advance")
        self.adv_options = read_advance_actuators(app.config_path)
        cur_mm = advance_mm_from_actuator(feeder.post_pick_actuator)
        self.adv_var = tk.StringVar(
            value=f"{cur_mm} mm" if cur_mm in self.adv_options
            else (f"{self.adv_options[0]} mm" if self.adv_options else ""))
        ttk.Combobox(frm, textvariable=self.adv_var, state="readonly",
                     values=[f"{mm} mm" for mm in self.adv_options],
                     width=10).grid(row=5, column=1, sticky="w", **pad)

        # Move before feed
        self.move_var = tk.BooleanVar(value=feeder.move_before_feed)
        tk.Checkbutton(
            frm, text="Move to feeder before feeding", variable=self.move_var,
            bg=app.COL_BG, fg=app.COL_TEXT, selectcolor=app.COL_BED,
            activebackground=app.COL_BG, activeforeground=app.COL_TEXT
        ).grid(row=6, column=1, sticky="w", **pad)

        # Manual feed — physically advance this feeder's tape now over serial,
        # independent of the saved Tape advance above.
        label(7, "Manual feed")
        feed_row = tk.Frame(frm, bg=app.COL_BG)
        feed_row.grid(row=7, column=1, sticky="w", **pad)
        self._manual_btns = []
        for mm in (4, 8, 12):
            b = tk.Button(feed_row, text=f"{mm} mm",
                          command=lambda mm=mm: self._manual_feed(mm))
            b.pack(side="left", padx=(0, 6))
            self._manual_btns.append(b)
        self.feed_status = tk.Label(frm, text="", bg=app.COL_BG, fg=app.COL_KEY,
                                    font=app.small, anchor="w", justify="left")
        self.feed_status.grid(row=8, column=1, sticky="w", padx=10)

        # Position preview (only meaningful when the slot is changed)
        self.preview = tk.Label(frm, text="", bg=app.COL_BED, fg=app.COL_TEXT,
                                justify="left", anchor="w", font=app.mono)
        self.preview.grid(row=9, column=0, columnspan=2, sticky="we", **pad)

        btns = tk.Frame(frm, bg=app.COL_BG)
        btns.grid(row=10, column=0, columnspan=2, sticky="e", **pad)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        tk.Button(btns, text="Save", command=self._save).pack(
            side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())
        self._update_preview()
        grab_when_visible(self)

    def _selection(self):
        try:
            return int(self.bank_var.get()), int(self.port_var.get())
        except (ValueError, AttributeError):
            return None, None

    def _manual_feed(self, mm):
        """Physically feed this feeder `mm` now (shares the app's feed core)."""
        def alive():
            try:
                return bool(self.winfo_exists())
            except tk.TclError:
                return False

        def on_start():
            if alive():
                for b in self._manual_btns:
                    b.config(state="disabled")
                self.feed_status.config(text=f"Feeding {mm} mm…")

        def on_progress(cmd):
            if alive():
                self.feed_status.config(text=f"→ {cmd}")

        def on_done(name, slot_n, advance_mm, err):
            if alive():
                for b in self._manual_btns:
                    b.config(state="normal")
                self.feed_status.config(
                    text=(f"Fed {advance_mm} mm (N{slot_n})." if err is None
                          else f"Feed failed: {err}"))
                if err is not None:
                    messagebox.showerror("Feed failed", str(err), parent=self)
            elif err is not None:
                messagebox.showerror("Feed failed", str(err))

        self.app.feed_feeder(self.feeder, mm, parent=self, on_start=on_start,
                             on_progress=on_progress, on_done=on_done)

    def _will_reposition(self) -> bool:
        return self.has_slots and \
            (self.bank_var.get(), self.port_var.get()) != self._init_slot

    def _unreachable(self, x, y):
        xmin, ymin, xmax, ymax = self.app.bed
        return not (xmin <= x <= xmax and ymin <= y <= ymax)

    def _other_at(self, bank, port):
        """A *different* feeder already occupying the target slot, if any."""
        return next((f for f in self.app.feeders
                     if f.fid != self.feeder.fid
                     and parse_slot(f.name) == (bank, port)), None)

    def _update_preview(self):
        if not self._will_reposition():
            self.preview.config(text="Slot unchanged — position kept as-is.")
            return
        bank, port = self._selection()
        if bank is None or bank not in self.app.models:
            self.preview.config(text="select a valid slot")
            return
        x, y, z, _ = self.app.models[bank].position(port)
        name = slot_name(bank, port)
        warn = "\n⚠ outside machine travel — NOT reachable." \
            if self._unreachable(x, y) else ""
        self.preview.config(
            text=(f"Will move to {name}\n"
                  f"X : {x:.2f} mm   Y : {y:.2f} mm\n"
                  f"Z : {z:.2f} mm{warn}"))

    def _save(self):
        raw_rot = self.rot_var.get().strip()
        try:
            rotation = float(raw_rot)
        except ValueError:
            messagebox.showerror(
                "Invalid value",
                f"Rotation in tape '{raw_rot}' is not a number.", parent=self)
            return
        m = re.match(r"\s*(\d+)", self.adv_var.get())
        if not m:
            messagebox.showerror(
                "Invalid value", "Pick a tape advance.", parent=self)
            return
        advance_mm = int(m.group(1))

        reposition = self._will_reposition()
        bank, port = self._selection()
        if reposition:
            if bank is None or bank not in self.app.models:
                messagebox.showerror(
                    "Invalid slot", "Select a valid slot.", parent=self)
                return
            x, y, _, _ = self.app.models[bank].position(port)
            if self._unreachable(x, y) and not messagebox.askyesno(
                "Not reachable",
                f"Slot {bank}/{port} is at X={x:.1f} Y={y:.1f}, outside the "
                "machine travel.\n\nMove it there anyway?", parent=self):
                return
            other = self._other_at(bank, port)
            if other and not messagebox.askyesno(
                "Slot in use",
                f"Slot {bank}/{port} is already used by '{other.name}'.\n"
                f"Move '{self.feeder.name}' there anyway?", parent=self):
                return

        part = self.part_var.get().strip() or "NC"
        self.destroy()
        self.app.edit_feeder(
            self.feeder, reposition=reposition, bank=bank or 0, port=port or 0,
            part=part, enabled=self.enabled_var.get(),
            rotation_in_feeder=rotation, advance_mm=advance_mm,
            move_before_feed=self.move_var.get())


class SettingsDialog(tk.Toplevel):
    """Configure the serial port + baud used to actuate feeders.

    The port dropdown lists the system's serial ports (via pyserial) but is
    editable, so a port that isn't auto-detected can be typed in. Settings are
    persisted to settings.json in the per-user config folder.
    """

    BAUDS = ("4800", "9600", "19200", "38400", "57600", "115200")

    def __init__(self, app: "FeederMapApp"):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.configure(bg=app.COL_BG)
        self.transient(app.root)
        self.resizable(False, False)
        self.minsize(420, 0)

        pad = {"padx": 10, "pady": 8}
        frm = tk.Frame(self, bg=app.COL_BG)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        tk.Label(frm, text="Feeder controller serial port", bg=app.COL_BG,
                 fg=app.COL_TEXT, font=("TkDefaultFont", 10, "bold")).grid(
                     row=0, column=0, columnspan=3, sticky="w", **pad)

        tk.Label(frm, text="Port", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=1, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar(value=app.settings.get("serial_port", ""))
        self.port_cb = ttk.Combobox(frm, textvariable=self.port_var,
                                    values=list_serial_ports(), width=26)
        self.port_cb.grid(row=1, column=1, sticky="we", **pad)
        tk.Button(frm, text="⟳", width=2, command=self._refresh_ports).grid(
            row=1, column=2, sticky="w", padx=(0, 10))

        tk.Label(frm, text="Baud", bg=app.COL_BG, fg=app.COL_TEXT).grid(
            row=2, column=0, sticky="e", **pad)
        self.baud_var = tk.StringVar(
            value=str(app.settings.get("baud", DEFAULT_BAUD)))
        ttk.Combobox(frm, textvariable=self.baud_var, values=self.BAUDS,
                     width=12).grid(row=2, column=1, sticky="w", **pad)

        tk.Label(frm, text="Not listed? Type the device path "
                           "(e.g. /dev/ttyUSB0, COM3).",
                 bg=app.COL_BG, fg=app.COL_KEY, font=app.small).grid(
                     row=3, column=0, columnspan=3, sticky="w", **pad)

        btns = tk.Frame(frm, bg=app.COL_BG)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", **pad)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        tk.Button(btns, text="Save", command=self._save).pack(
            side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())
        grab_when_visible(self)

    def _refresh_ports(self):
        self.port_cb["values"] = list_serial_ports()

    def _save(self):
        port = self.port_var.get().strip()
        raw = self.baud_var.get().strip()
        try:
            baud = int(raw)
        except ValueError:
            messagebox.showerror(
                "Invalid baud", f"'{raw}' is not a number.", parent=self)
            return
        self.app.settings["serial_port"] = port
        self.app.settings["baud"] = baud
        try:
            save_settings(self.app.settings)
        except Exception as exc:           # noqa: BLE001 - surface any failure
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self.destroy()
        self.app.status.config(
            text=f"Settings saved  ·  port {port or '—'} @ {baud}")


class RotationHelpDialog(tk.Toplevel):
    """Visual guide: how to read 'rotation in tape' from a CAD/tape pair.

    Follows OpenPnP's EIA-481 convention: tape 0° is sprocket holes on top,
    + is CCW, − is CW. The value is the part's rotation in the tape pocket
    relative to its upright orientation in the E-CAD library footprint.
    """

    # Each row: (rotation°, one-line caption explaining what the user sees)
    EXAMPLES = (
        (0,    "Part sits in the tape the same way as in CAD."),
        (-90,  "Part rotated 90° clockwise in the tape (pin 1 → top-right)."),
        (180,  "Part rotated 180° in the tape (pin 1 → bottom-right)."),
        (90,   "Part rotated 90° counter-clockwise (pin 1 → bottom-left)."),
    )

    BODY_W = 28          # half-width of the chip in px
    BODY_H = 18          # half-height of the chip in px
    DOT_R = 3            # pin-1 dot radius
    CANVAS_W = 110
    CANVAS_H = 80

    def __init__(self, app: "FeederMapApp", parent: tk.Toplevel | None = None):
        super().__init__(parent if parent is not None else app.root)
        self.app = app
        self.title("Rotation in tape — guide")
        self.configure(bg=app.COL_BG)
        self.transient(parent if parent is not None else app.root)
        self.resizable(False, False)

        pad = {"padx": 12, "pady": 6}
        intro = (
            "Rotation in tape = how the part is turned inside the tape pocket,\n"
            "measured against its upright orientation in your CAD library.\n"
            "\n"
            "  1. View the part upright in CAD — pin 1 / pol. mark = ●.\n"
            "  2. View the tape with sprocket holes on TOP (EIA-481).\n"
            "  3. The angle from CAD-upright → tape orientation is the value.\n"
            "     Positive = CCW, negative = CW."
        )
        tk.Label(self, text=intro, bg=app.COL_BG, fg=app.COL_TEXT,
                 justify="left", anchor="w", font=app.mono).pack(
                     anchor="w", **pad)

        # header row
        grid = tk.Frame(self, bg=app.COL_BG)
        grid.pack(fill="x", padx=12, pady=(2, 4))
        for col, text in enumerate(("CAD (library)", "", "Tape", "Enter")):
            tk.Label(grid, text=text, bg=app.COL_BG, fg=app.COL_TEXT,
                     font=("TkDefaultFont", 10, "bold")).grid(
                         row=0, column=col, padx=8, pady=(0, 4))

        for r, (rot, caption) in enumerate(self.EXAMPLES, start=1):
            cad = tk.Canvas(grid, width=self.CANVAS_W, height=self.CANVAS_H,
                            bg=app.COL_BED, highlightthickness=0)
            cad.grid(row=r, column=0, padx=8, pady=4)
            self._draw_chip(cad, rot=0, with_sprocket=False)

            tk.Label(grid, text="→", bg=app.COL_BG, fg=app.COL_TEXT,
                     font=("TkDefaultFont", 14)).grid(
                         row=r, column=1, padx=4)

            tape = tk.Canvas(grid, width=self.CANVAS_W, height=self.CANVAS_H,
                             bg=app.COL_BED, highlightthickness=0)
            tape.grid(row=r, column=2, padx=8, pady=4)
            self._draw_chip(tape, rot=rot, with_sprocket=True)

            tk.Label(grid, text=f"{rot:+d}°", bg=app.COL_BG, fg=app.COL_SEL,
                     font=("TkDefaultFont", 12, "bold")).grid(
                         row=r, column=3, padx=8)
            tk.Label(grid, text=caption, bg=app.COL_BG, fg=app.COL_TEXT,
                     font=app.small, anchor="w", justify="left").grid(
                         row=r, column=4, sticky="w", padx=(8, 8))

        note = (
            "Note: if your OpenPnP is older than 2022-06-10, its 0° was "
            "sprocket-holes-on-the-left,\n"
            "not on top — add 90° to the value above to match the old "
            "convention."
        )
        tk.Label(self, text=note, bg=app.COL_BG, fg=app.COL_NOPART,
                 justify="left", anchor="w", font=app.small).pack(
                     anchor="w", padx=12, pady=(8, 4))

        btns = tk.Frame(self, bg=app.COL_BG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        grab_when_visible(self)

    # -- drawing helpers ------------------------------------------------- #
    @staticmethod
    def _rotate_visual_ccw(dx: float, dy: float,
                           deg: float) -> tuple[float, float]:
        """Rotate (dx, dy) visually CCW by `deg` in tkinter screen coords
        (Y axis points down). Positive deg = CCW as seen by the user."""
        r = math.radians(deg)
        c, s = math.cos(r), math.sin(r)
        return (dx * c + dy * s, -dx * s + dy * c)

    def _draw_chip(self, canvas: tk.Canvas, rot: float, with_sprocket: bool):
        """Draw a small IC-like body with a pin-1 dot, rotated by `rot`
        degrees CCW. If with_sprocket, also draw three sprocket holes on top
        so the tape's 0° (EIA-481) orientation is visually clear."""
        app = self.app
        cx, cy = self.CANVAS_W / 2, self.CANVAS_H / 2 + 8

        if with_sprocket:
            # tape edge strip
            canvas.create_rectangle(8, 4, self.CANVAS_W - 8, 18,
                                    fill=app.COL_BG, outline=app.COL_BED_EDGE)
            for sx in (cx - 22, cx, cx + 22):
                canvas.create_oval(sx - 3, 8, sx + 3, 14,
                                   fill=app.COL_BED, outline=app.COL_TEXT)

        # body polygon (rotated)
        w, h = self.BODY_W, self.BODY_H
        corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
        pts = []
        for dx, dy in corners:
            rx, ry = self._rotate_visual_ccw(dx, dy, rot)
            pts.extend((cx + rx, cy + ry))
        canvas.create_polygon(pts, fill="#3b3f55",
                              outline=app.COL_BED_EDGE, width=1)

        # pin-1 dot — anchored to the part's top-left in CAD, rotates with it
        pad = 6
        pdx, pdy = self._rotate_visual_ccw(-w + pad, -h + pad, rot)
        dx0, dy0 = cx + pdx, cy + pdy
        canvas.create_oval(dx0 - self.DOT_R, dy0 - self.DOT_R,
                           dx0 + self.DOT_R, dy0 + self.DOT_R,
                           fill="#ffd33d", outline="")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="View and edit OpenPnP Bamboo feeders.")
    ap.add_argument("config", nargs="?", default=DEFAULT_CONFIG,
                    help=f"path to machine.xml (default: {DEFAULT_CONFIG})")
    args = ap.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"error: config not found: {args.config}", file=sys.stderr)
        return 1

    ensure_config_dir()        # create the per-user data folder if it's missing
    FeederMapApp(args.config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
