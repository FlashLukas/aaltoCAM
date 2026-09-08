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
    for node in doc.nodes.values():
        raw = node.params.get("path")
        if raw and os.path.isabs(raw):
            try:
                node.params["path"] = os.path.relpath(raw, base)
            except ValueError:
                pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(doc))
    doc.base_dir = base


def load(path: str) -> Document:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    doc = Document.from_dict({"nodes": data.get("node", [])})
    doc.base_dir = os.path.dirname(os.path.abspath(path))
    return doc
