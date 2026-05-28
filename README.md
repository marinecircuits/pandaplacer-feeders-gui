# Bambu Feeder Map

A small Tkinter app that reads your OpenPnP `machine.xml` and draws a top-down
map of where every **Bambu feeder** (`BambooFeederAutoVision`) sits on the
machine bed.

## Run

```bash
python3 feeder_map.py                 # reads ~/.openpnp2/machine.xml
python3 feeder_map.py /path/machine.xml
```

No dependencies beyond the Python standard library (Tkinter ships with Python).

## What you see

- The bed rectangle is sized from the X/Y axis soft limits in the config
  (here 318 × 343 mm), with a 50 mm grid and the `0,0` origin marked.
- Each feeder is a square at its real X/Y, labelled with the tail of its name
  (e.g. `N205`). Colours:
  - **green** — enabled and has a part assigned
  - **amber** — enabled but no part (`NC`)
  - **grey** — disabled
- **Hover** a feeder to see its details; **click** to pin them in the side panel.
- **Double-click** a feeder to toggle it enabled/disabled (writes to the config,
  with the same backup / OpenPnP-closed rules as the other edits).
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
into the `<feeders>` container. Banks are discovered from the data, so the four
current banks are:

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

## Moving a feeder

Select a feeder and click **⇄ Move selected**. Pick a new slot (bank) + feeder
(port); the feeder is repositioned onto that preset and renamed to match
(`PPBF-N…`), keeping its id, part, pipeline and enabled state. Unreachable or
already-used target slots are flagged for confirmation.

## Changing the part

Select a feeder and click **🏷 Set part** to reassign its associated part from
the `parts.xml` dropdown (prefilled with the current part). Only the feeder's
`part-id` is changed.

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
