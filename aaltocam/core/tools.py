"""The tool library: one place to record what each cutter actually wants.

A tool carries its geometry (diameter, flutes, V-bit angle) and the feeds and
depth that have been shown to work with it. Operations pick a tool instead of
having its diameter typed into one node and its feeds into another.

Spindle speed is recorded but never commanded. On a Wegstr the controller's
G-code parser has no S word at all -- the spindle is set by hand -- so the rpm
here is written into the file as a comment beside the tool change, where the
operator will see it when the machine stops, and used to work out chip load.
Postprocessors for machines that do command spindle speed use it normally.

Feeds of zero mean "not established yet". A tool with unset feeds still gives
its diameter to the geometry; the CNC job falls back to its own fields and says
so, rather than inventing a number.

The library is a TOML file in the user's config directory, shared by every
project, because the same cutters get used across boards. Point AALTOCAM_TOOLS at
another file to override.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field

KINDS = ("mill", "endmill", "router", "drill", "vbit")


@dataclass
class Tool:
    id: str
    name: str = ""
    kind: str = "mill"
    diameter: float = 0.2
    flutes: int = 2
    tip_angle: float = 0.0          # V-bits only, degrees
    tip_diameter: float = 0.0       # V-bits only, mm
    feed_xy: float = 0.0            # mm/min, 0 = not established
    feed_z: float = 0.0             # mm/min, 0 = not established
    cut_z: float = 0.0              # mm, negative, 0 = not established
    depth_per_pass: float = 0.0     # mm, positive, 0 = single pass
    spindle_rpm: int = 0            # advisory; 0 = not recorded
    notes: str = ""
    source: str = ""

    @property
    def shape(self) -> str:
        return "v" if self.kind == "vbit" else "flat"

    @property
    def has_feeds(self) -> bool:
        return self.feed_xy > 0 and self.feed_z > 0

    def label(self) -> str:
        bits = [f"{self.diameter:g} mm", self.kind]
        if not self.has_feeds:
            bits.append("no feeds yet")
        return f"{self.id}  ({', '.join(bits)})"

    def chip_load(self, feed: float | None = None) -> float | None:
        """mm per tooth at the recorded spindle speed, or None if unknowable."""
        feed = self.feed_xy if feed is None else feed
        if not (feed > 0 and self.spindle_rpm > 0 and self.flutes > 0):
            return None
        return feed / (self.spindle_rpm * self.flutes)


#: Shipped library. The four with feeds were measured from jobs that cut real
#: boards; the rest carry geometry only, waiting for feeds to be established.
SEED: list[Tool] = [
    Tool(id="lpkf-rf-0.15", name="LPKF RF mill 0.15", kind="mill", diameter=0.15,
         flutes=2, feed_xy=120, feed_z=60, cut_z=-0.06,
         source="measured: 10x10SampleChipHolderIP isolation"),
    Tool(id="lpkf-rf-0.25", name="LPKF RF mill 0.25", kind="mill", diameter=0.25,
         flutes=2),
    Tool(id="lpkf-end-0.8", name="LPKF end mill 0.8", kind="endmill", diameter=0.8,
         flutes=2),
    Tool(id="lpkf-end-1.0", name="LPKF end mill 1.0", kind="endmill", diameter=1.0,
         flutes=2, feed_xy=60, feed_z=60, cut_z=-1.0,
         source="measured: 10x10SampleChipHolderIP hole milling"),
    Tool(id="router-0.6", name="Spiral router 0.6", kind="router", diameter=0.6,
         flutes=2),
    Tool(id="router-1.0", name="Spiral router 1.0", kind="router", diameter=1.0,
         flutes=2, feed_xy=60, feed_z=60, cut_z=-1.6, depth_per_pass=0.75,
         source="measured: 10x10SampleChipHolderIP cutout"),
    Tool(id="drill-0.7", name="Spiral drill 0.7", kind="drill", diameter=0.7,
         flutes=2, feed_xy=60, feed_z=100, cut_z=-1.0,
         source="measured: 10x10SampleChipHolderIP drilling"),
] + [
    Tool(id=f"drill-{d:.1f}", name=f"Spiral drill {d:.1f}", kind="drill", diameter=d, flutes=2)
    for d in (0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 0.9, 1.0, 1.1)
]

HEADER = """\
# aaltocam tool library
#
# One entry per physical cutter. Geometry is used by the operations; feeds are
# used by the CNC job when "Take feeds from the tool" is on.
#
# feed_xy, feed_z   mm/min. Zero means not established yet -- the job falls back
#                   to its own fields and warns instead of guessing.
# cut_z             mm, negative. depth_per_pass mm, positive, 0 = single pass.
# spindle_rpm       advisory. A Wegstr ignores S entirely and is set by hand;
#                   this is written as a comment at the tool change and used for
#                   the chip-load figure.
# kind              mill | endmill | router | drill | vbit
"""


def config_path() -> str:
    """Where the shared library lives, honouring AALTOCAM_TOOLS."""
    override = os.environ.get("AALTOCAM_TOOLS")
    if override:
        return os.path.abspath(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "aaltocam", "tools.toml")


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(tools: list[Tool]) -> str:
    out = [HEADER]
    for tool in tools:
        out.append("[[tool]]")
        for key, value in asdict(tool).items():
            out.append(f"{key} = {_fmt(value)}")
        out.append("")
    return "\n".join(out)


@dataclass
class Library:
    tools: list[Tool] = field(default_factory=list)
    path: str = ""

    def ids(self) -> list[str]:
        return [tool.id for tool in self.tools]

    def get(self, tool_id: str) -> Tool | None:
        if not tool_id:
            return None
        for tool in self.tools:
            if tool.id == tool_id:
                return tool
        return None

    def by_diameter(self, diameter: float, kind: str | None = None,
                    tolerance: float = 0.001) -> Tool | None:
        """Closest tool of that diameter, for matching a rest-machining group
        back to the cutter it will be run with."""
        best, best_gap = None, tolerance
        for tool in self.tools:
            if kind and tool.kind != kind:
                continue
            gap = abs(tool.diameter - diameter)
            if gap <= best_gap:
                best, best_gap = tool, gap
        return best

    def save(self, path: str | None = None):
        path = path or self.path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dumps(self.tools))
        self.path = path


def load(path: str | None = None, create: bool = True) -> Library:
    """Read the library, writing the seed file the first time round."""
    path = path or config_path()
    if not os.path.isfile(path):
        library = Library([Tool(**asdict(tool)) for tool in SEED], path)
        if create:
            try:
                library.save(path)
            except OSError:
                pass
        return library

    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    known = set(Tool.__dataclass_fields__)
    tools = []
    for entry in data.get("tool", []):
        clean = {k: v for k, v in entry.items() if k in known}
        if not clean.get("id"):
            continue
        try:
            tools.append(Tool(**clean))
        except TypeError:
            continue
    return Library(tools, path)


_LIBRARY: Library | None = None
#: Mutated in place so that parameter descriptors built at import time keep
#: pointing at the current set of tool ids.
CHOICES: list[str] = [""]


def library(reload: bool = False) -> Library:
    global _LIBRARY
    if _LIBRARY is None or reload:
        _LIBRARY = load()
        CHOICES[:] = [""] + _LIBRARY.ids()
    return _LIBRARY


def get(tool_id: str) -> Tool | None:
    return library().get(tool_id)
