"""Project persistence.

Projects are TOML: readable, diffable, and safe to keep in git next to the
KiCad project they came from. Paths are stored relative to the project file
so a whole board directory can be moved or shared.
"""

from __future__ import annotations

import os
import tomllib

from .graph import Document


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_fmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    text = str(value)
    if "\n" in text:
        return '"""\n' + text.replace('"""', '\\"\\"\\"') + '"""'
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(doc: Document) -> str:
    out = ["# aaltocam project", "version = 1", ""]
    if doc.source:
        out.append("[source]")
        for key in sorted(doc.source):
            out.append(f"{key} = {_fmt(doc.source[key])}")
        out.append("")
    for node_id in doc.order:
        node = doc.nodes[node_id]
        out.append("[[node]]")
        out.append(f"id = {_fmt(node.id)}")
        out.append(f"op = {_fmt(node.op)}")
        out.append(f"name = {_fmt(node.name)}")
        out.append(f"inputs = {_fmt(node.inputs)}")
        out.append(f"visible = {_fmt(node.visible)}")
        out.append("")
        out.append("[node.params]")
        for key in sorted(node.params):
            out.append(f"{key} = {_fmt(node.params[key])}")
        out.append("")
    return "\n".join(out)


def save(doc: Document, path: str):
    base = os.path.dirname(os.path.abspath(path))

    def rebase(stored: str) -> str:
        """Re-express a stored path relative to where the project is going.

        Paths already in the document are relative to the directory it was
        built in, which is not necessarily where it is being saved. Resolving
        against the old base before relativising to the new one is what makes
        "save as" into another folder keep working; without it every Gerber
        reference quietly points somewhere that does not exist.
        """
        if not stored:
            return stored
        absolute = stored
        if not os.path.isabs(stored) and doc.base_dir:
            absolute = os.path.normpath(os.path.join(doc.base_dir, stored))
        try:
            return os.path.relpath(absolute, base)
        except ValueError:
            # A different drive on Windows: nothing relative can reach it.
            return absolute

    for node in doc.nodes.values():
        if "path" in node.params:
            node.params["path"] = rebase(node.params["path"])
    # The board this was plotted from travels with the project, so Re-plot
    # still works after closing and reopening it.
    if doc.source.get("kicad_pcb"):
        doc.source["kicad_pcb"] = rebase(doc.source["kicad_pcb"])

    os.makedirs(base, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(doc))
    doc.base_dir = base


def load(path: str) -> Document:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    doc = Document.from_dict({"nodes": data.get("node", []),
                              "source": data.get("source", {})})
    doc.base_dir = os.path.dirname(os.path.abspath(path))
    return doc


def board_path(doc: Document) -> str:
    """Absolute path of the .kicad_pcb this project was plotted from, if any."""
    board = doc.source.get("kicad_pcb")
    if not board:
        return ""
    if os.path.isabs(board) or not doc.base_dir:
        return board
    return os.path.normpath(os.path.join(doc.base_dir, board))
