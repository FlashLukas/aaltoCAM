"""Parameter descriptors.

Every operation declares its parameters here once. The GUI builds its editing
form from these descriptors, the CLI validates project files against them, and
the cache hashes them. Adding a parameter to an operation needs no GUI code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Param:
    name: str
    label: str
    default: Any
    kind: str = "float"  # float | int | bool | str | choice | path
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float = 0.05
    decimals: int = 3
    choices: list = field(default_factory=list)
    help: str = ""
    group: str = ""
    # Name of another parameter whose value enables this one, e.g. "multidepth".
    depends_on: str | None = None

    def coerce(self, value):
        if self.kind == "tools":
            return normalize_tools(value)
        if self.kind == "shapes":
            return normalize_shapes(value)
        try:
            if self.kind == "float":
                return float(value)
            if self.kind == "int":
                return int(value)
            if self.kind == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
        except (TypeError, ValueError):
            return self.default
        return value


def F(name, label, default, **kw):
    return Param(name, label, default, "float", **kw)


def I(name, label, default, **kw):
    return Param(name, label, default, "int", **kw)


def B(name, label, default, **kw):
    return Param(name, label, default, "bool", **kw)


def S(name, label, default, **kw):
    return Param(name, label, default, "str", **kw)


def C(name, label, default, choices, **kw):
    return Param(name, label, default, "choice", choices=choices, **kw)


def SH(name, label, default, **kw):
    """A drawn shape list: [{"type": "rect"|"poly", "points": [[x, y], ...]}, ...]."""
    return Param(name, label, default, "shapes", **kw)


def T(name, label, default, **kw):
    """A tool list: [{"dia": 0.8, "shape": "flat", ...}, ...], largest first."""
    return Param(name, label, default, "tools", **kw)


def P(name, label, default="", **kw):
    return Param(name, label, default, "path", **kw)


DEFAULT_TOOL = {"dia": 0.8, "shape": "flat", "tip_dia": 0.02, "tip_angle": 30.0}


def normalize_tools(value) -> list[dict]:
    """Coerce whatever came out of a TOML file or a table widget into a
    clean, largest-first tool list."""
    out = []
    for entry in value or []:
        if isinstance(entry, (int, float)):
            entry = {"dia": float(entry)}
        if not isinstance(entry, dict):
            continue
        tool = dict(DEFAULT_TOOL)
        tool.update({k: entry[k] for k in DEFAULT_TOOL if k in entry})
        tool["dia"] = float(tool["dia"])
        tool["tip_dia"] = float(tool["tip_dia"])
        tool["tip_angle"] = float(tool["tip_angle"])
        tool["shape"] = "v" if str(tool["shape"]).lower().startswith("v") else "flat"
        if tool["dia"] > 0:
            out.append(tool)
    out.sort(key=lambda t: -t["dia"])
    return out or [dict(DEFAULT_TOOL)]


def normalize_shapes(value) -> list[dict]:
    """Clean a drawn-shape list. Rectangles keep two corner points, polygons
    keep three or more."""
    out = []
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        kind = "rect" if str(entry.get("type", "rect")).startswith("r") else "poly"
        points = []
        for point in entry.get("points", []):
            try:
                points.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError, IndexError):
                continue
        if kind == "rect" and len(points) >= 2:
            out.append({"type": "rect", "points": points[:2]})
        elif kind == "poly" and len(points) >= 3:
            out.append({"type": "poly", "points": points})
    return out
