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
Point aaltoCAM at it for the session:

```
# PowerShell
$env:AALTOCAM_TOOLS = "<clone>\examples\tools\wegstr-lpkf.toml"
# bash
export AALTOCAM_TOOLS=<clone>/examples/tools/wegstr-lpkf.toml
```

The jobs below use these tools. Their feeds were measured on boards cut on that
machine:

| job | tool id | cutter | XY / Z feed | depth |
|---|---|---|---|---|
| isolation | `lpkf-rf-0.15` | LPKF RF end mill 0.15 mm | 120 / 60 | −0.06 |
| drilling | `drill-0.2` … `drill-1.1` | Wegstr TiN drill set, matched by hole size | 0.7 mm: 60 / 100 | 0.7 mm: −1.0, others −1.8 in 0.6 pecks |
| hole milling (≥ 2 mm) | `lpkf-end-1.0` | LPKF end mill 1.0 mm | 60 / 60 | −1.0 |
| cutout | `router-1.0` | Wegstr 1.0 mm spiral-patterned router | 60 / 60 | −1.6 in 0.75 passes |

Only the 0.7 mm drill has measured feeds. Every other drill size runs on the
job's own fields, 60 mm/min plunge to −1.8 mm in 0.6 mm pecks. The V-bits and
the LPKF micro and universal cutters have assumed tip sizes, so do not choose
them unless the person asks.

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
| `PROBLEM: deepest cut is Z.. which does not get through a .. mm board` | the depth measured for that tool is shallower than this board | stop and ask. Use `--thickness` only if the person confirms the board really is thinner |
| `note: reaches X-1.00 Y-1.00` | the outside cutout runs a tool radius plus margin beyond the edge | pass it on: zero the machine with that much room to the left and in front |
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
3. **Hole milling**, if not empty: LPKF end mill 1.0 mm.
4. **Cutout, last**: 1.0 mm router. The tabs hold the board in place.

Notes for the operator, to include word for word:

- Zero X and Y at the bottom-left corner of the board outline. Set Z zero on the
  copper surface, and set it again after every tool change.
- Leave the Wegstr's own surface levelling on. These files have flat Z because
  the machine compensates for the surface itself.
- Set the spindle to 11000 rpm by hand. The controller ignores the `S` word.
- Run the first job of a new setup on scrap and watch it.

### Two-sided boards

`--side bottom` mirrors the bottom layers in place about the outline, so it
lines up with the top only if the board is turned over about that same line.
This script does not make alignment pins. For a real two-sided board, tell the
person to use **Edit → Set up the bottom side** in the GUI, which drills
registration holes on the flip axis and mirrors about them. Do not try to do
that by hand here.
