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
- **Filter** box matches feeder name or part id.
- **Show disabled** toggles the (many) disabled reserve slots.
- **⟳ Reload** re-reads the config after you change it in OpenPnP.

The current config has two banks: the left column (`PPBF-N0…N112`) and the
right column (`PPBF-N200…N312`), each stacked along Y.
