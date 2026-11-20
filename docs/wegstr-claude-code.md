# Wegstr G-code with Claude Code

A handout for a coding agent. Give it this file and a board, and it can take a
KiCad board to checked Wegstr G-code for isolation, drilling, hole milling and
the board cutout, then tell the operator what to load and in what order.

**What to tell your agent:**

> Read docs/wegstr-claude-code.md in https://github.com/FlashLukas/aaltoCAM and
> make Wegstr G-code for `<path to board.kicad_pcb>`. Report every problem and
> warning before handing me files.

You need Windows, macOS or Linux with Python 3.12 or newer, git, and KiCad 7 or
later when starting from a `.kicad_pcb`. A folder of already plotted Gerbers
works without KiCad.

---

## For the agent

### Rules

- **Make files, nothing else.** Do not send anything to the machine, start the
  Wegstr software, or touch a serial port. A person loads the files and runs
  them.
- **Do not edit the board.** If the board needs changing, say what and where,
  and let the person change it in KiCad.
- **Do not make a failing check pass.** Do not raise `--thickness`, shrink the
  clearance or change the tool library to get a clean run. A `PROBLEM` line is
  something for the person to decide on, so stop and report it.
- **Do not invent feeds.** Tools with zero feeds in the library are not
  established yet. If a job warns about that, pass the warning on.
- Quote every line starting with `PROBLEM`, `warning:` or `short near`
  word for word in your report.

### 1. Install, once

```
git clone https://github.com/FlashLukas/aaltoCAM
cd aaltoCAM
python -m pip install -e .
```

That is the headless core: gerbonara and Shapely. Add `.[gui]` instead of `.`
if the person also wants to look at the result in the GUI. On Windows, PySide6
can fail to install inside a deeply nested folder because of the 260-character
path limit. If that happens, make the virtual environment in a short path such
as `C:\venv`.

Check with `python -c "import aaltocam; print(aaltocam.__version__)"`.

### 2. Use the Wegstr tool library

`examples/tools/wegstr-lpkf.toml` holds the cutters of one real Wegstr setup.
`examples/wegstr_job.py` uses it automatically, and its report starts with a
`tools:` line naming the file. Check that line. Pass `--tools <file>` only if
the person has their own library with the same ids.

Do not rely on setting `AALTOCAM_TOOLS` in one shell command and running
aaltoCAM in the next. If each of your commands starts a fresh shell, the
variable is gone by then.

**For the GUI and the plain `aaltocam` command**, which do not go through the
script, install the library once as the default. It lives at
`%APPDATA%\aaltocam\tools.toml` on Windows and `~/.config/aaltocam/tools.toml`
elsewhere. If a file is already there, ask the person before replacing it, and
keep a copy:

```
# PowerShell
$dir = "$env:APPDATA\aaltocam"
New-Item -ItemType Directory -Force $dir | Out-Null
if (Test-Path "$dir\tools.toml") { Copy-Item "$dir\tools.toml" "$dir\tools.toml.bak" }
Copy-Item "<clone>\examples\tools\wegstr-lpkf.toml" "$dir\tools.toml"
```

After that, `aaltocam-gui <board>-top.toml` shows the same tools the script
used. If the GUI is already open, use **Tools → Reload tool library**. Without
this step the GUI falls back to its built-in seed library. That library has no
`router-0.6`, so a cutout using it shows no tool.

The jobs below use these tools. Their feeds were measured on boards cut on that
machine:

| job | tool id | cutter | XY / Z feed | depth |
|---|---|---|---|---|
| isolation | `lpkf-rf-0.15` | LPKF RF end mill 0.15 mm | 120 / 60 | −0.06 |
| drilling | `drill-0.2` … `drill-1.1` | Wegstr TiN drill set, matched by hole size | 0.7 mm: 60 / 100 | 0.7 mm: −1.0, others −1.8 in 0.6 pecks |
| hole milling (≥ 2 mm) | `lpkf-end-1.0` | LPKF end mill 1.0 mm | 60 / 60 | −1.0 |
| cutout | `router-1.0` | Wegstr 1.0 mm spiral-patterned router | 60 / 60 | −1.6 in 0.75 passes |

Only the 0.7 mm drill has measured feeds. Every other drill size runs on the
job's own fields, 60 mm/min plunge to −1.8 mm in 0.6 mm pecks. Holes larger
than the largest drill (1.1 mm) are milled rather than drilled, so no file
asks for a drill that is not in the set.

`lpkf-rf-0.25` and `router-0.6` exist but have no measured feeds. The script
refuses to use them until feeds are given on the command line (see the Rogers
example below). The V-bits and the LPKF micro and universal cutters have
assumed tip sizes, so do not choose them unless the person asks.

### 3. Generate

```
python examples/wegstr_job.py <board.kicad_pcb> -o <board folder>/gcode
```

| option | default | meaning |
|---|---|---|
| `--side top\|bottom` | `top` | which copper side; `bottom` mirrors every layer about Y |
| `--thickness` | `1.6` | board thickness in mm; drilling, hole milling and the cutout must reach it |
| `--passes` | `2` | isolation passes; more passes widen the gap around each track |
| `--tabs 0\|2\|4\|8` | `4` | holding tabs on the cutout |
| `--through-depth` | each tool's own | cut drilling, hole milling and the cutout to this depth (mm) instead |
| `--mill-above` | just above the largest drill (1.15) | holes larger than this are milled, not drilled |
| `--isolation-feed`, `--isolation-plunge`, `--isolation-depth` | the tool's | XY feed, Z feed (mm/min) and depth (mm, positive) for isolation |
| `--cutout-feed`, `--cutout-plunge`, `--cutout-step` | the tool's | XY feed, Z feed (mm/min) and depth per pass (mm) for the cutout |
| `--isolation-tool`, `--cutout-tool`, `--hole-mill-tool` | as in the table | other library ids |
| `--kicad-cli` | found automatically | path to `kicad-cli` if it is not found |
| `--replot` | off | re-plot even if the existing plot looks current |

Starting from a `.kicad_pcb`, the board is plotted with KiCad's own plotter
into `<board>-aaltocam-plot/` beside the board file. Tell the person that folder
appears. Every layer is moved so the bottom-left corner of the board outline is
X0 Y0.

It writes, into the output folder:

- `<board>-<side>.toml`, the project. `aaltocam-gui <that file>` opens it for a
  look, and `aaltocam <that file> -o <dir>` reproduces the same G-code.
- One `.nc` file per job, named `<board>-<side>-cnc_job_<n>.nc`. The numbers
  depend on what the board has: a board without a drill file has no drilling
  or hole milling jobs. The report prints each file's path with the job's name
  (Isolation, Drill, Hole milling, Cutout) on the line under it, so go by that.

The script exits 0 and prints `clean` only when no check failed.

### 4. Read the report

Each job gets a line like this:

```
Isolation job: lpkf-rf-0.15, 141 lines, ~3 min, lowest Z -0.060, top feed 120, X -0.06..17.70 Y -0.16..15.57
```

Check that the lowest Z is what the table above says for that tool, and that
the top feed is at most 170. The Wegstr cannot go faster, rapids included.

What each finding means, and what to do:

| output | meaning | do |
|---|---|---|
| `clearance: N short(s)` with `short near X.. Y..` | the 0.15 mm cutter cannot get between two separate pieces of copper there, so they stay connected | stop. Give the person the coordinates (they are machine coordinates, measured from the outline's bottom-left corner). The fix is more clearance in KiCad. Rounded corners are harmless |
| `PROBLEM: spans .. more than the Wegstr's 140 x 200 mm travel` | the job does not fit the machine | stop. If the X and Y spans swapped would fit 140 × 200, suggest rotating the board 90° in KiCad; otherwise it cannot be cut in one setup |
| `PROBLEM: deepest cut is Z.. which does not get through a .. mm board` | the depth measured for that tool is shallower than this board | stop and ask. With the person's agreement, pass `--through-depth` a little deeper than the board and make sure there is a sacrificial plate under it. Lower `--thickness` only if the board really is thinner |
| `note: reaches X-1.00 Y-1.00` | the outside cutout runs a tool radius plus margin beyond the edge | pass it on: zero the machine with that much room to the left and in front |
| `PROBLEM: <tool> has no measured feeds` | isolation or cutout would run on placeholder numbers | stop and ask for feeds, or use the numbers in the Rogers example if the person agrees |
| `warning: N hole(s) of X mm have no drill of that size` | KiCad asked for a size the set does not have, e.g. 1.02 | pass it on with the nearest drill named; the operator fits that one at the pause |
| `warning: Tool .. has no feeds recorded yet` | that job used its own feed fields | pass it on |
| an empty job (`lowest Z 0.000`) | nothing to do, e.g. no holes of 2 mm or more | say it can be skipped |

Then open each `.nc` file and read the start of it. It should begin with the
job name and the tool in comments, then `G21`, `G90`, `G00 Z2`, and a comment
naming the cutter and `11000 rpm - set by hand`. There should be one G word per
line, and no parentheses except whole-line comments.

### 5. Hand over

Report the files in the order they should run, with the cutter for each:

1. **Isolation**: LPKF RF 0.15 mm.
2. **Drilling**: one file, smallest drill first. Start with the drill named
   in the comments at the top. At each `T<n> M06` + `M00` pause, fit the drill
   named in the two comment lines just above it.
3. **Hole milling**, if not empty: LPKF end mill 1.0 mm, for every hole above
   1.1 mm.
4. **Cutout, last**: the router named in the file's header. The tabs hold the
   board in place.

Notes for the operator, to include word for word:

- Zero X and Y at the bottom-left corner of the board outline. Set Z zero on the
  copper surface, and set it again after every tool change.
- Leave the Wegstr's own surface levelling on. These files have flat Z because
  the machine compensates for the surface itself.
- Set the spindle to 11000 rpm by hand. The controller ignores the `S` word.
- Run the first job of a new setup on scrap and watch it.
- If the cut goes deeper than the board, as in the Rogers example below, the
  board must sit on a sacrificial plate. The tools go that far into whatever is
  under it.

### Example: 1.7 mm Rogers, cut and drilled to 2 mm

Isolation with the 0.15 mm RF mill, all holes and the cutout to 2.0 mm, cutout
with the 0.6 mm spiral router:

```
python examples/wegstr_job.py <board.kicad_pcb> -o <board folder>/gcode ^
    --thickness 1.7 --through-depth 2.0 ^
    --cutout-tool router-0.6 --cutout-feed 50 --cutout-plunge 20 --cutout-step 0.25
```

(`^` continues a line in cmd. Use a backtick in PowerShell, or `\` in bash.)

For the 0.25 mm RF mill instead, add
`--isolation-tool lpkf-rf-0.25 --isolation-feed 120 --isolation-plunge 60 --isolation-depth 0.06`.
Those are the 0.15 mm mill's measured numbers, used as a starting point.

What this does and why:

- **Depth.** Drilling, hole milling and the cutout go to −2.0 mm: through
  1.7 mm and 0.3 mm into the plate below. Drills peck at 0.6 mm and the hole
  mill steps 0.3 mm. Given depths take over from the tools' measured ones,
  which were set for thinner stock.
- **0.6 mm router.** 50 mm/min XY, 20 mm/min plunge and 0.25 mm per pass are
  conservative starting values, not measured ones. Eight passes to 2.0 mm make
  the cutout slow; that is expected. **Check the router's flute length is at
  least 2.0 mm.** A shorter flute rubs the shank in the slot and snaps the
  tool.
- **Rogers.** Ceramic-filled laminates (RO4000) are abrasive and wear cutters
  faster than FR4. PTFE laminates (RT/duroid) are soft and leave burrs. Either
  way, watch the first cutout and slow down if the router labours.

Report all of this to the person as starting values to try on scrap first.

### Two-sided boards

`--side bottom` mirrors the bottom layers in place about the outline, so it
lines up with the top only if the board is turned over about that same line.
This script does not make alignment pins. For a real two-sided board, tell the
person to use **Edit → Set up the bottom side** in the GUI, which drills
registration holes on the flip axis and mirrors about them. Do not try to do
that by hand here.
