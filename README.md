# PandaPlacer Bamboo Feeders

A small Tkinter app to **view and edit** the **Bamboo feeders**
(`BambooFeederAutoVision`) in your OpenPnP `machine.xml`. It draws a top-down
map of where every feeder sits on the machine bed, and lets you add, move,
remove, re-part, enable/disable, set the tape rotation/advance of feeders, and
**actuate a feeder over serial** to perform a feed.

## Run

```bash
python3 pandaplacer_feeders.py                 # reads ~/.openpnp2/machine.xml
python3 pandaplacer_feeders.py /path/machine.xml
```

Viewing and editing the config needs only the Python standard library (Tkinter
ships with Python). The **Perform feed** feature additionally needs
[`pyserial`](https://pypi.org/project/pyserial/) to talk to the feeder
controller — install it with `pip install pyserial`. Without it the rest of the
app still works; only the feed button reports that pyserial is missing.

## What you see

- The bed rectangle is sized from the X/Y axis soft limits in the config
  (here 318 × 343 mm), with a 50 mm grid and the `0,0` origin marked.
- Each feeder is a square at its real X/Y, labelled with its slot number
  zero-padded to 3 digits (e.g. `N000`, `N011`, `N205`). The number is drawn
  next to the marker — on the right of the label for left-side feeders, on the
  left for right-side ones — so the numbers line up against the bed, with the
  part id (if any) on the other side. Colours:
  - **green** — enabled and has a part assigned
  - **amber** — enabled but no part (`NC`)
  - **grey** — disabled
- **Hover** a feeder to see its details; **click** to pin them in the side
  panel. The detail panel is a two-column grid (name/part, position & rotation,
  tape advance & move-before-feed, feed count, id) that stays aligned in any
  font.
- **Double-click** a feeder to open the **Edit feeder** dialog and change all
  its properties at once (see *Editing a feeder*).
- **Filter** box matches feeder name or part id.
- **Show disabled** toggles the (many) disabled reserve slots.
- **⟳ Reload** re-reads the config after you change it in OpenPnP.

The current config has two banks: the left column (`PPBF-N0…N112`) and the
right column (`PPBF-N200…N312`), each stacked along Y.

## Adding a feeder (preset by slot + feeder number)

Click **＋ Add feeder**. Instead of typing coordinates you pick:

- **Slot** = bank `0–3`
- **Feeder** = port `0–12`

The position is computed from a per-bank grid model the app derives from the
feeders already in the config (rail X, a 12.5 mm port pitch, and a median
intercept — robust to a few calibrated/off-grid feeders). The dialog previews
the resulting name (`PPBF-N<bank><port>`), X/Y/Z and rotation live, and warns if
that name already exists. Pick a **Part** (dropdown of `parts.xml` ids) and
**Enabled**, then **Add**.

The new feeder clones the vision pipeline and settings from a feeder in the
same bank (preferring an enabled one), gets a fresh unique id, and is inserted
into the `<feeders>` container.

### Tape rotation is remembered / auto-applied

When you pick a part, the dialog decides the new feeder's tape orientation
(`rotation-in-feeder`) for you and shows it in the preview (and again in the
confirmation after adding):

1. **Remembered** — if that part is in `tape_orientations.json` (see below),
   that value is reused. The preview reads *"remembered"*.
2. **Auto 0603/0805 R/C** — otherwise, 0603 and 0805 resistors and capacitors
   (part ids containing `R0603`/`C0603`/`R0805`/`C0805`) default to **−90°**.
   Inductors (`L_0603`) and other 0603-sized parts (`0603_LED`) are excluded.
3. Otherwise the clone keeps the template feeder's own value.

A remembered orientation always wins over the −90° default, so a part you once
configured differently keeps that orientation.

#### The orientation map (`tape_orientations.json`)

Preferred orientations live in a simple JSON file — a flat `part-id → rotation`
map:

```json
{
  "R_0603_1608Metric-10k_0603": "90",
  "SOT-23-3-BSS138": "180"
}
```

It is stored in the shared PandaPlacer per-user config folder the app creates
inside the OS config directory (so it survives moving/updating the script).
This folder is general — other PandaPlacer configs may live here too:

| OS            | Path |
|---------------|------|
| Linux / Unix  | `$XDG_CONFIG_HOME/pandaplacer-feeders-gui/` (default `~/.config/pandaplacer-feeders-gui/`) |
| macOS         | `~/Library/Application Support/pandaplacer-feeders-gui/` |
| Windows       | `%APPDATA%\pandaplacer-feeders-gui\` |

It is updated automatically whenever an orientation is applied — both when a
feeder is **added** (the resolved value, including the −90° R/C default) and
when you set the tape rotation in the **Edit feeder** dialog. You can also edit
it by hand; it is written sorted for clean diffs. Entries persist even after the
feeder is removed, so re-adding the same part reuses its orientation.

### Actuator value follows the slot

Each Bamboo auto-feeder is addressed by its **slot number** — the number in the
`PPBF-N<num>` name (`bank*100 + port`). So both `feed-actuator-value` and
`post-pick-actuator-value` are set to that number when a feeder is added (e.g.
`PPBF-N305` → `305.0`, `PPBF-N7` → `7.0`), instead of inheriting the cloned
template's slot. The Add preview shows the value as `Act : …`. The actuator
*names* (`AutoFeeder_4mm/8mm/12mmAdvance`) depend on the tape width, not the
slot, so they are kept as cloned.

### Tape advance is remembered / auto-applied

The tape **advance** — how far the tape steps after a pick — is the
`post-pick-actuator-name` (`AutoFeeder_4mm/8mm/12mmAdvance`, from the
`AutoFeeder_<N>mmAdvance` actuators the machine defines). Like the tape
rotation it is a property of the part's tape, so it is remembered per part in
`tape_advances.json` (a sibling of `tape_orientations.json`, a flat
`part-id → advance-mm` map) and auto-applied when you add a feeder for that
part. The Add preview shows it as `Adv : … mm (remembered)`; if the part has no
remembered advance, the cloned template's advance is kept (`(template
default)`). It is saved/updated whenever you set the advance in the **Edit
feeder** dialog (see *Editing a feeder*) or add a feeder for a part with a
remembered value.

Banks are discovered from the data, so the four current banks are:

| Slot | Side  | Ports     | X (mm) | Y at port 0 → 12 |
|------|-------|-----------|--------|------------------|
| 0    | left  | N0–N12    | 11.30  | 291.40 → 141.40 (−12.5/port) |
| 1    | left  | N100–N112 | 11.30  | 126.60 → −23.40 (−12.5/port) |
| 2    | right | N200–N212 | 307.20 | −21.00 → 129.00 (+12.5/port) |
| 3    | right | N300–N312 | 307.20 | 144.00 → 294.00 (+12.5/port) |

The same backup / OpenPnP-closed rules below apply. A preset whose computed
position falls outside the machine travel (the X/Y soft limits — e.g. the
bottom slots `N111/N112` and `N200/N201` at Y < 0) is flagged as **not
reachable by the head**, and you're asked to confirm before adding it.

## Editing a feeder

**Double-click** a feeder to open **Edit feeder** — the one place to change
every editable property. All fields are applied together in a single backup and
write:

- **Slot** (bank / port) — the position is only rewritten if you actually
  change the slot. Leaving it alone keeps the feeder's exact (possibly
  calibrated) X/Y; changing it repositions and renames to the new preset
  (`PPBF-N…`) and updates the actuator value to the new slot number (see
  *Actuator value follows the slot*), so a moved feeder drives the right
  physical feeder. Unreachable or already-used target slots are flagged for
  confirmation. If there are no bank presets, every field except the slot is
  still editable.
- **Part** — reassign from the `parts.xml` dropdown (prefilled with the
  current part). Type to filter the list.
- **Enabled** — enable/disable the feeder.
- **Tape rotation** — the part's orientation in the tape (`rotation-in-feeder`,
  separate from the pick location's rotation). A combobox of common values
  (`-90 / 0 / 90 / 180 / 270`); any numeric value is accepted, and a **?**
  button opens a visual guide. The chosen orientation is saved to
  `tape_orientations.json` so future feeders for the same part reuse it.
- **Tape advance** — a dropdown of the advance distances the machine defines
  (`2 / 4 / 8 / 12 mm`, read from its `AutoFeeder_<N>mmAdvance` actuators). This
  rewrites `post-pick-actuator-name`; the post-pick value (the slot number) is
  left untouched. The chosen advance is saved to `tape_advances.json` so future
  feeders for the same part reuse it.
- **Move to feeder before feeding** — the `move-before-feed` attribute.

## Performing a feed (serial)

Select a feeder and click **▶ Perform feed** to physically actuate it through
the PPBFC AS feeder controller over a serial port. The command is addressed and
sized from the feeder itself:

- **Port N** = the feeder's slot number (`bank*100 + port`, the number in its
  name and its actuator value).
- **Advance** = the feeder's tape advance in mm (from `post-pick-actuator-name`).

After you confirm, the app opens the configured serial port, sends the feed
sequence, then closes the port. The sequence mirrors the *PPBFC AS Feeder Test
Tool*'s Activate + Single Advance:

```
M610 S1                 ; enable controller
M611 S0                 ; deactivate all ports
M611 N<slot> S1         ; activate this port
M600 N<slot> F<mm> X1   ; advance <mm> mm
M611 N<slot> S0         ; deactivate this port
```

The serial I/O runs off the UI thread, and the status bar shows progress and
the result. You'll get a clear message if the port isn't configured, the
feeder has no tape advance set, the slot can't be determined from the name, or
`pyserial` isn't installed.

## Settings

Click **⚙ Settings** to choose the **serial port** and **baud** (default
`19200`) used by *Perform feed*. The port dropdown lists detected serial ports
but is editable, so you can type a device path that isn't auto-detected (e.g.
`/dev/ttyUSB0`, `COM3`). Settings are saved to `settings.json` in the same
per-user config folder as the orientation/advance maps.

## Removing a feeder

Select a feeder, then click **🗑 Remove selected** to permanently delete it
from `machine.xml`. Notes:

- **Close OpenPnP first.** OpenPnP rewrites `machine.xml` when it exits, so if
  it is running it will overwrite (and may undo) the deletion.
- A timestamped backup of the whole config is written to `config_backups/`
  before anything is changed — restore from there if needed.
- The edit is surgical: only the selected `<feeder>` block is cut, the rest of
  the file is left byte-for-byte unchanged, and the result is validated as
  well-formed XML before being written.
