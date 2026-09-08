"""The parametric document model.

A project is a directed acyclic graph of nodes. Every node stores the
operation that produced it and the parameters it was produced with -- nothing
is ever baked. Editing a parameter marks that node and everything downstream
dirty; the next evaluation recomputes only what actually changed.

This is the one thing FlatCAM cannot do: there, parameters are consumed at
creation time and discarded, so a Geometry or CNC Job object is a dead end.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .params import Param


# --------------------------------------------------------------------------
# Operation registry
# --------------------------------------------------------------------------


@dataclass
class Operation:
    name: str
    label: str
    params: list[Param]
    # Accepted payload kind per input slot. A leading "?" marks the slot
    # optional, so a node evaluates fine with nothing connected to it.
    inputs: list[str]
    output: str  # payload kind produced
    func: Callable[..., Any]
    category: str = "general"
    input_labels: list[str] = field(default_factory=list)

    def slot_label(self, slot: int) -> str:
        if slot < len(self.input_labels):
            return self.input_labels[slot]
        return "Input" if len(self.inputs) == 1 else f"Input {slot + 1}"

    def slot_optional(self, slot: int) -> bool:
        return self.inputs[slot].startswith("?")

    def slot_kind(self, slot: int) -> str:
        return self.inputs[slot].lstrip("?")

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None


REGISTRY: dict[str, Operation] = {}


def register(name, label, params, inputs, output, category="general", input_labels=None):
    def wrap(func):
        REGISTRY[name] = Operation(name, label, params, inputs, output, func, category,
                                   list(input_labels or []))
        return func

    return wrap


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


@dataclass
class Payload:
    """Result of evaluating a node.

    kind is one of: 'copper' (polygons), 'drills', 'paths' (toolpaths),
    'gcode' (text).
    """

    kind: str
    data: Any
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Nodes and document
# --------------------------------------------------------------------------


@dataclass
class Node:
    id: str
    op: str
    name: str = ""
    params: dict = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    visible: bool = True

    def operation(self) -> Operation:
        return REGISTRY[self.op]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "op": self.op,
            "name": self.name,
            "inputs": list(self.inputs),
            "visible": self.visible,
            "params": dict(self.params),
        }

    @staticmethod
    def from_dict(d: dict) -> "Node":
        node = Node(
            id=d["id"],
            op=d["op"],
            name=d.get("name", ""),
            params=dict(d.get("params", {})),
            inputs=list(d.get("inputs", [])),
            visible=d.get("visible", True),
        )
        # Fill in any parameter added since the project was saved.
        op = REGISTRY.get(node.op)
        if op:
            for key, value in op.defaults().items():
                node.params.setdefault(key, value)
        return node


class EvalError(RuntimeError):
    pass


class Document:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []
        self._cache: dict[str, Payload] = {}
        self._keys: dict[str, str] = {}
        self.base_dir: str | None = None
        #: Where this graph came from, so it can be regenerated later: the
        #: .kicad_pcb it was plotted from, and which side was built. Carried
        #: through save/load and undo, unlike anything held only by the GUI.
        self.source: dict = {}

    # -- structure ---------------------------------------------------------

    def new_id(self, op: str) -> str:
        i = 1
        while f"{op}_{i}" in self.nodes:
            i += 1
        return f"{op}_{i}"

    def add(self, op: str, inputs: list[str] | None = None, name: str = "", **params) -> Node:
        if op not in REGISTRY:
            raise KeyError(f"unknown operation: {op}")
        operation = REGISTRY[op]
        node = Node(
            id=self.new_id(op),
            op=op,
            name=name or operation.label,
            params={**operation.defaults(), **params},
            inputs=list(inputs or []),
        )
        self.nodes[node.id] = node
        self.order.append(node.id)
        return node

    def remove(self, node_id: str):
        self.nodes.pop(node_id, None)
        if node_id in self.order:
            self.order.remove(node_id)
        for node in self.nodes.values():
            node.inputs = [i for i in node.inputs if i != node_id]
        self.invalidate()

    def dependents(self, node_id: str) -> list[str]:
        """Every node downstream of node_id, including itself."""
        found = {node_id}
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.id in found:
                    continue
                if any(i in found for i in node.inputs):
                    found.add(node.id)
                    changed = True
        return [i for i in self.order if i in found]

    def set_param(self, node_id: str, key: str, value):
        node = self.nodes[node_id]
        if node.params.get(key) == value:
            return False
        node.params[key] = value
        self.invalidate(node_id)
        return True

    def invalidate(self, node_id: str | None = None):
        if node_id is None:
            self._cache.clear()
            self._keys.clear()
            return
        for dep in self.dependents(node_id):
            self._cache.pop(dep, None)
            self._keys.pop(dep, None)

    # -- evaluation --------------------------------------------------------

    def cache_key(self, node_id: str, seen: set | None = None) -> str:
        seen = seen or set()
        if node_id in seen:
            raise EvalError(f"cycle detected at {node_id}")
        seen = seen | {node_id}
        node = self.nodes[node_id]
        payload = {
            "op": node.op,
            "params": {k: node.params[k] for k in sorted(node.params)},
            "inputs": [self.cache_key(i, seen) if i in self.nodes else "" for i in node.inputs],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def evaluate(self, node_id: str) -> Payload:
        key = self.cache_key(node_id)
        if self._keys.get(node_id) == key and node_id in self._cache:
            return self._cache[node_id]

        node = self.nodes[node_id]
        operation = node.operation()
        args = []
        for slot in range(len(operation.inputs)):
            source = node.inputs[slot] if slot < len(node.inputs) else ""
            if not source or source not in self.nodes:
                if operation.slot_optional(slot):
                    args.append(None)
                    continue
                raise EvalError(f"{node.name}: {operation.slot_label(slot).lower()} not connected")
            payload = self.evaluate(source)
            want = operation.slot_kind(slot)
            if want != "any" and payload.kind != want:
                raise EvalError(f"{node.name}: expected {want} input, got {payload.kind}")
            args.append(payload)

        result = operation.func(self, node, *args)
        if not isinstance(result, Payload):
            result = Payload(operation.output, result)
        self._cache[node_id] = result
        self._keys[node_id] = key
        return result

    def evaluate_all(self) -> dict[str, Payload | Exception]:
        out: dict[str, Payload | Exception] = {}
        for node_id in self.order:
            try:
                out[node_id] = self.evaluate(node_id)
            except Exception as exc:  # keep going; the GUI reports per-node
                out[node_id] = exc
        return out

    def is_cached(self, node_id: str) -> bool:
        try:
            return self._keys.get(node_id) == self.cache_key(node_id)
        except Exception:
            return False

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {"version": 1, "source": dict(self.source),
                "nodes": [self.nodes[i].to_dict() for i in self.order]}

    @staticmethod
    def from_dict(d: dict) -> "Document":
        doc = Document()
        doc.source = dict(d.get("source", {}))
        for entry in d.get("nodes", []):
            node = Node.from_dict(entry)
            doc.nodes[node.id] = node
            doc.order.append(node.id)
        return doc
