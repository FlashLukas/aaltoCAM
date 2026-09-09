"""Open a KiCad board by asking KiCad to plot it.

Reading geometry out of a .kicad_pcb directly is a trap. The file stores design
intent, not copper: tracks are centrelines with a width, pads are shapes that
still need the footprint's position, rotation and side applied, and zones are
only as good as the last time somebody hit "fill". Getting any of that subtly
wrong produces a board outline that still looks plausible and copper that is
half a millimetre off, with nothing to announce it.

So we don't. `kicad-cli` is KiCad's own plotter, and it produces exactly the
Gerbers the plot dialog would. We call it, then hand the result to the plot
folder importer that already exists.

The plot lands in a folder beside the board rather than in a temp directory, so
that a saved project keeps working and re-plotting after a board edit is just
running this again.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

from .discover import build_board
from .graph import Document

#: Where kicad-cli hides when it is not on PATH.
SEARCH_GLOBS = [
    r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
    r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/usr/bin/kicad-cli",
]

#: Flags kept in one place, because they are the part most likely to need a
#: tweak against a future KiCad. Documented for KiCad 7 through 10.
GERBER_FLAGS = ["--no-protel-ext"]
DRILL_FLAGS = ["--format", "excellon", "-u", "mm",
               "--drill-origin", "absolute",
               "--excellon-zeros-format", "decimal"]

PLOT_SUFFIX = "-aaltocam-plot"
TIMEOUT = 180


class KicadCliMissing(RuntimeError):
    """kicad-cli could not be found."""


class PlotFailed(RuntimeError):
    """kicad-cli ran and was unhappy. Carries the command and its stderr."""


def find_cli(explicit: str | None = None) -> str:
    """Locate kicad-cli: an explicit path, then PATH, then the usual places."""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise KicadCliMissing(f"No kicad-cli at {explicit}")

    found = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if found:
        return found

    candidates: list[str] = []
    for pattern in SEARCH_GLOBS:
        candidates.extend(glob.glob(pattern))
    if candidates:
        # Highest version number wins, so KiCad 9 beats a leftover KiCad 7.
        def version_key(path):
            return [int(part) if part.isdigit() else part
                    for part in re.split(r"(\d+)", path)]
        return sorted(candidates, key=version_key)[-1]

    raise KicadCliMissing(
        "kicad-cli was not found. It ships with KiCad 7 and later; either put "
        "KiCad's bin folder on PATH or point aaltocam at kicad-cli directly."
    )


def cli_version(cli: str | None = None) -> str:
    """The version string kicad-cli reports, or '' if it will not say."""
    try:
        out = subprocess.run([cli or find_cli(), "version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return ""


def board_layers(pcb_path: str) -> list[str]:
    """Copper layers actually defined in the board, in stack order.

    Read straight out of the file's layer table -- this is metadata, not
    geometry, so parsing it here is safe in a way that parsing copper is not.
    """
    try:
        with open(pcb_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(200_000)
    except OSError:
        return ["F.Cu", "B.Cu"]

    start = text.find("(layers")
    if start < 0:
        return ["F.Cu", "B.Cu"]
    names = re.findall(r'\(\s*\d+\s+"([^"]+)"\s+(signal|user|power|mixed|jumper)',
                       text[start:start + 20_000])
    copper = [name for name, kind in names
              if kind in ("signal", "power", "mixed", "jumper") and name.endswith(".Cu")]
    return copper or ["F.Cu", "B.Cu"]


def plot_dir_for(pcb_path: str) -> str:
    stem = os.path.splitext(os.path.basename(pcb_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(pcb_path)), stem + PLOT_SUFFIX)


def _is_stale(pcb_path: str, outdir: str) -> bool:
    """True when the plot is missing or older than the board file."""
    if not os.path.isdir(outdir):
        return True
    produced = [os.path.join(outdir, name) for name in os.listdir(outdir)]
    produced = [path for path in produced if os.path.isfile(path)]
    if not produced:
        return True
    board_mtime = os.path.getmtime(pcb_path)
    return min(os.path.getmtime(path) for path in produced) < board_mtime


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PlotFailed(f"kicad-cli did not finish within {TIMEOUT} s:\n"
                         + " ".join(command))
    if done.returncode != 0:
        raise PlotFailed(
            "kicad-cli failed:\n  " + " ".join(command)
            + "\n" + (done.stderr or done.stdout or "").strip()
        )
    return done.stdout


def plot(pcb_path: str, outdir: str | None = None, layers: list[str] | None = None,
         cli: str | None = None, force: bool = False,
         separate_th: bool = False) -> tuple[str, list[str]]:
    """Plot a .kicad_pcb to Gerber + Excellon and return (directory, notes).

    Copper layers plus Edge.Cuts by default. Drills come out as one combined
    file, because splitting plated from non-plated only matters to a fab house
    and the importer would take just one of the two.

    An up-to-date plot is left alone unless `force` is set.
    """
    pcb_path = os.path.abspath(pcb_path)
    if not os.path.isfile(pcb_path):
        raise FileNotFoundError(pcb_path)
    outdir = outdir or plot_dir_for(pcb_path)
    notes: list[str] = []

    if not force and not _is_stale(pcb_path, outdir):
        notes.append(f"Plot in {os.path.basename(outdir)} is newer than the board; reused.")
        return outdir, notes

    binary = find_cli(cli)
    # Outer copper only by default: you cannot mill an inner layer, and plotting
    # them just fills the import report with files nothing will use.
    outer = [name for name in board_layers(pcb_path) if name in ("F.Cu", "B.Cu")]
    chosen = layers or (outer or ["F.Cu"]) + ["Edge.Cuts"]
    os.makedirs(outdir, exist_ok=True)

    _run([binary, "pcb", "export", "gerbers", "-o", outdir,
          "-l", ",".join(chosen), *GERBER_FLAGS, pcb_path])
    drill_flags = list(DRILL_FLAGS) + (["--excellon-separate-th"] if separate_th else [])
    # KiCad 7's drill exporter rejects an output path without a trailing
    # separator -- "Output must be a directory" even when it plainly is one.
    # Harmless on later versions, so always send it that way.
    _run([binary, "pcb", "export", "drill", "-o", outdir + os.sep, *drill_flags, pcb_path])

    version = cli_version(binary)
    notes.append(f"Plotted {', '.join(chosen)} with {version or os.path.basename(binary)}")
    notes.append(f"into {os.path.basename(outdir)}")
    return outdir, notes


def open_board(pcb_path: str, side: str = "top", cli: str | None = None,
               force: bool = False, origin: bool = False) -> tuple[Document, list[str]]:
    """Plot a .kicad_pcb and build the same starting graph as a plot folder."""
    outdir, notes = plot(pcb_path, cli=cli, force=force)
    doc, more = build_board(outdir, side, origin=origin)
    # Remember the board, so the project can be re-plotted after it is saved,
    # closed and opened again.
    doc.source["kicad_pcb"] = os.path.abspath(pcb_path)
    more.append("Coordinates follow KiCad's absolute origin, so the board sits "
                "wherever it sat on the sheet. Add a Transform set to move to "
                "zero if you want it at the machine origin.")
    return doc, notes + more
