# Attribution

aaltoCAM is a small program standing on some substantial ones. This file records
what it uses, who wrote it, and under what terms. Versions are the ones aaltoCAM
was developed and tested against; the licences were read from the installed
packages rather than from memory.

aaltoCAM's own code imports exactly three third-party packages. Everything else
below arrives underneath them.

## Libraries aaltoCAM imports

**gerbonara** 1.5.0 — Apache License 2.0
Jan Sebastian Götte (jaseg) and XenGi — <https://gitlab.com/gerbolyze/gerbonara>

Reads Gerber and Excellon. aaltoCAM would not exist without it: parsing Gerber
correctly is most of the work in a CAM tool, and gerbonara does it properly,
including apertures, macros, arcs and the Excellon dialects.

Two functions are reimplemented in `aaltocam/core/geometry.py` rather than
called, because the 1.5.0 versions are broken:

- `ArcPoly.approximate_arcs` calls `segments` as a method when it is a
  property, and references unbound names. Any board with a rectangular pad
  failed to load.
- `Rectangle.to_arc_poly` returns an axis-aligned bounding box for a rotated
  rectangle. A 2 × 1 mm pad at 0.3 rad came out 71 % oversized.

Both replacements were written after reading gerbonara's source to work out
what the correct behaviour was, so they are derived from Apache-2.0 code and
that licence's notice requirements apply to them. Worth reporting upstream.

**Shapely** 2.1.2 — BSD 3-Clause
Sean Gillies and contributors — <https://github.com/shapely/shapely>

Every offset, union, buffer and difference in the toolpath code. The isolation
routing, the cutout compensation and the even-odd outline recovery are all
Shapely operations with a thin layer of intent on top. Shapely bundles **GEOS**
(LGPL-2.1), whose licence ships alongside it as `LICENSE_GEOS`.

**PySide6-Essentials** and **shiboken6** 6.11.2 —
LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
The Qt for Python team, The Qt Company — <https://pyside.org>

The entire GUI: the board view, the parameter forms, the icons, which are
painted with `QPainter` rather than shipped as images. **This is the licence
with real obligations attached** — see the note at the end.

## Arriving underneath those

Pulled in automatically, not imported by aaltoCAM:

- **NumPy** (BSD-3-Clause) — via Shapely
- **Rtree** (MIT) and the **Flask / Quart / Hypercorn** stack with **click**,
  **Jinja2**, **MarkupSafe**, **Werkzeug**, **itsdangerous**, **blinker**,
  **h11**, **h2**, **hpack**, **hyperframe**, **priority**, **wsproto**,
  **aiofiles** — via gerbonara, which offers a web viewer aaltoCAM never calls

## Tools, not dependencies

**KiCad** — GPL-3.0-or-later — <https://gitlab.com/kicad/code/kicad>
`kicad-cli` is run as a separate process to plot `.kicad_pcb` files to Gerber
and Excellon. No KiCad code is linked or included. Reading copper out of a
`.kicad_pcb` directly would mean reimplementing KiCad's own understanding of
tracks, pads and zone fills, and getting it subtly wrong; asking KiCad to plot
is the honest way round.

**PyInstaller** — GPL-2.0-or-later, with a bootloader exception that permits
freezing applications under any licence — <https://github.com/pyinstaller/pyinstaller>
Used only to build the standalone Windows folder.

**setuptools**, **pytest** — build and test, not shipped.

## Prior art

**FlatCAM** by Juan Pablo Caram — <https://bitbucket.org/jpcgt/flatcam>
aaltoCAM exists to replace FlatCAM for this workflow, and owes it the shape of
the problem: isolation routing, tool-side compensation, holding tabs and
cutout as separate operations are all FlatCAM's framing of PCB milling.

**No FlatCAM code was read or used.** Its role here was as a reference
*output*: G-code and dimensions produced by FlatCAM from the same boards were
used to check that aaltoCAM's Gerber loader agreed — copper measured
12.7990 × 27.5134 mm against FlatCAM's implied 12.799 × 27.513.

**Wegstr CNC** — <https://www.wegstr.com>
The Wegstr postprocessor was written from observation of the machine's own
control software: which G and M codes its parser accepts, that it reads only
the first G word on a line and has no `S` word, the 170 mm/min feed ceiling,
the 0.004 mm step, and the serial framing. No vendor code is included or
redistributed here — only a description of the behaviour the machine already
exhibits to anyone who sends it G-code.

## A licensing note, from someone who is not a lawyer

Running aaltoCAM from source on your own machine raises nothing. **Distributing
the frozen `aaltocam.exe` is different**, because it bundles Qt, and Qt is here
under the LGPL. The usual LGPL expectations are that the licence text travels
with the binary, that you say Qt is used under LGPLv3, and that a recipient can
relink against their own Qt build. The PyInstaller `onedir` layout helps with
the last point, since the Qt DLLs sit in the folder as separate files rather
than being welded into one executable — but whether that satisfies the licence
is a judgement, not a fact, and not one to take from this file.

If the executable is only ever copied to your own mill PC, none of this
arises. If it is published for other people to download, the licence texts of
at least Qt, GEOS, gerbonara, Shapely and NumPy should travel with it, and it
is worth a look from someone who does this for a living.

aaltoCAM is licensed under the Apache License, Version 2.0. The text is in
`LICENSE` and the attribution required by section 4(d) is in `NOTICE`.

Apache-2.0 was chosen partly because the gerbonara-derived functions in
`aaltocam/core/geometry.py` are already under it, so that notice obligation is
carried by the project's own licence rather than bolted on beside it. gerbonara
(Apache-2.0), Shapely (BSD-3) and NumPy (BSD-3) are permissive and constrain
nothing further. Qt's LGPL, described above, shapes what may be done with the
frozen executable rather than with this source.
