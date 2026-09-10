"""Choices that outlive one project.

Deliberately not stored in the project file. Which theme the window is drawn in
belongs to the person sitting in front of it, not to the board they happen to
have open, and a project copied to the machine beside the mill should not drag
somebody else's colours along with it.

The file sits next to the tool library, and the directory is taken from there
rather than worked out again, so the two cannot disagree about where a user's
configuration lives.
"""

from __future__ import annotations

import os
import tomllib

from . import tools as toollib

THEMES = ("dark", "light")

DEFAULTS = {"theme": "dark"}


def config_path() -> str:
    return os.path.join(os.path.dirname(toollib.config_path()), "settings.toml")


def load(path: str | None = None) -> dict:
    """Settings, with defaults for anything missing, unreadable or invalid.

    A settings file that has been hand-edited into nonsense must never stop the
    program starting. The worst it can cost is the wrong colours, and finding
    that out is a great deal cheaper than an application that will not open.
    """
    values = dict(DEFAULTS)
    try:
        with open(path or config_path(), "rb") as fh:
            stored = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return values

    if isinstance(stored, dict):
        values.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if values.get("theme") not in THEMES:
        values["theme"] = DEFAULTS["theme"]
    return values


def save(values: dict, path: str | None = None) -> str:
    """Write the settings, returning where they went."""
    target = path or config_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)

    lines = ["# aaltocam settings. Written by the program, safe to edit by hand.",
             ""]
    for key in sorted(values):
        value = values[key]
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        else:
            lines.append(f"{key} = {value}")

    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


def set_theme(name: str, path: str | None = None) -> str:
    if name not in THEMES:
        raise ValueError(f"unknown theme: {name}")
    values = load(path)
    values["theme"] = name
    return save(values, path)
