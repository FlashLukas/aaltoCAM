"""What the control channel is allowed to do, and getting it onto the GUI thread.

Two jobs here. The first is the vocabulary: a fixed set of verbs, each one a
thing the window can already do, with no verb that runs code. The second is
threading, which is where this would otherwise go wrong quietly -- the socket
answers on a worker thread, and touching a Qt widget or the document from there
corrupts state in ways that surface minutes later as an unrelated crash.

So nothing in the worker thread touches the document. A request is put on a
queue, a timer on the GUI thread picks it up, runs it where everything else
runs, and hands the answer back. The worker waits. That costs a few
milliseconds and removes a whole category of bug.

Every verb that changes anything pushes an undo step first, so a command you
did not want is Ctrl+Z away, exactly like a change made by hand.
"""

from __future__ import annotations

import queue
import time

from ..core import REGISTRY, ops
from ..core import project as project_io

#: How long a caller waits for the GUI thread to come round to its request.
TIMEOUT_S = 20.0


class Bridge:
    """Hands a command from the socket thread to the GUI thread and back."""

    def __init__(self):
        self._pending: queue.Queue = queue.Queue()

    def submit(self, command: dict) -> dict:
        """Called on the socket thread. Blocks until the GUI thread answers."""
        answer: queue.Queue = queue.Queue(maxsize=1)
        self._pending.put((command, answer))
        try:
            return answer.get(timeout=TIMEOUT_S)
        except queue.Empty:
            return {"ok": False,
                    "error": f"the window did not answer within {TIMEOUT_S:g} s"}

    def pump(self, run):
        """Called on the GUI thread by a timer. Runs whatever is waiting."""
        deadline = time.monotonic() + 0.05      # never hold the GUI for long
        while time.monotonic() < deadline:
            try:
                command, answer = self._pending.get_nowait()
            except queue.Empty:
                return
            try:
                answer.put(run(command))
            except Exception as exc:
                answer.put({"ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})


def _node_state(window, node_id: str) -> dict:
    node = window.doc.nodes[node_id]
    result = window.results.get(node_id)
    state = {
        "id": node_id,
        "op": node.op,
        "name": node.name,
        "inputs": list(node.inputs),
        "visible": node.visible,
        "params": dict(node.params),
    }
    if isinstance(result, Exception):
        state["error"] = str(result)
    elif result is not None:
        state["kind"] = result.kind
        state["meta"] = {k: v for k, v in result.meta.items()
                         if isinstance(v, (int, float, str, bool, list, tuple))}
        bounds = ops.payload_bounds(result)
        if bounds:
            state["bounds"] = [round(v, 4) for v in bounds]
    return state


class Commands:
    """The vocabulary. One method per verb, and nothing that executes code."""

    def __init__(self, window):
        self.window = window

    # -- reading -----------------------------------------------------------

    def do_ping(self, _c):
        return {"ok": True, "project": self.window.path,
                "nodes": len(self.window.doc.order),
                "dirty": self.window._dirty}

    def do_operations(self, _c):
        """What can be added, and the parameters each one takes."""
        return {"ok": True, "operations": [
            {"op": name,
             "label": operation.label,
             "category": operation.category,
             "inputs": list(operation.inputs),
             "input_labels": list(operation.input_labels),
             "output": operation.output,
             "params": [{"name": p.name, "label": p.label, "default": p.default}
                        for p in operation.params]}
            for name, operation in sorted(REGISTRY.items())]}

    def do_list(self, _c):
        return {"ok": True, "nodes": [_node_state(self.window, n)
                                      for n in self.window.doc.order]}

    def do_get(self, command):
        node_id = self._require(command, "id")
        return {"ok": True, "node": _node_state(self.window, node_id)}

    def do_gcode(self, command):
        """The text of a CNC job, to read before anyone cuts it."""
        node_id = self._require(command, "id")
        result = self.window.results.get(node_id)
        if not hasattr(result, "kind") or result.kind != "gcode":
            return {"ok": False, "error": f"{node_id} has not produced G-code"}
        return {"ok": True, "gcode": result.data,
                "lines": len(result.data.splitlines())}

    # -- changing ----------------------------------------------------------

    def do_set(self, command):
        node_id = self._require(command, "id")
        params = command.get("params")
        if not isinstance(params, dict) or not params:
            return {"ok": False, "error": "set needs a params object"}
        node = self.window.doc.nodes[node_id]
        unknown = [k for k in params if node.operation().param(k) is None]
        if unknown:
            return {"ok": False,
                    "error": f"{node.op} has no parameter(s): {', '.join(unknown)}"}

        self.window.push_undo()
        for key, value in params.items():
            param = node.operation().param(key)
            self.window.doc.set_param(node_id, key, param.coerce(value))
        self.window.doc.invalidate(node_id)
        self.window.recompute()
        self.window.form.show_node(self.window.doc, node)
        return {"ok": True, "node": _node_state(self.window, node_id)}

    def do_add(self, command):
        op = self._require(command, "op")
        if op not in REGISTRY:
            return {"ok": False, "error": f"unknown operation: {op}"}
        self.window.push_undo()
        node = self.window.doc.add(op, list(command.get("inputs", [])),
                                   name=str(command.get("name", "")),
                                   **dict(command.get("params", {})))
        self.window.refresh_list(select=node.id)
        self.window.recompute()
        return {"ok": True, "node": _node_state(self.window, node.id)}

    def do_connect(self, command):
        node_id = self._require(command, "id")
        slot = int(command.get("slot", 0))
        source = str(command.get("source", ""))
        if source and source not in self.window.doc.nodes:
            return {"ok": False, "error": f"no such node: {source}"}
        self.window._on_input_changed(node_id, slot, source)
        return {"ok": True, "node": _node_state(self.window, node_id)}

    def do_rename(self, command):
        node_id = self._require(command, "id")
        self.window._on_rename(node_id, str(command.get("name", "")))
        return {"ok": True, "node": _node_state(self.window, node_id)}

    def do_delete(self, command):
        node_id = self._require(command, "id")
        self.window.push_undo()
        self.window.view.clear_node(node_id)
        self.window.doc.remove(node_id)
        self.window.refresh_list()
        self.window.recompute()
        return {"ok": True, "deleted": node_id}

    def do_visible(self, command):
        node_id = self._require(command, "id")
        self.window.doc.nodes[node_id].visible = bool(command.get("visible", True))
        self.window.refresh_list(select=node_id)
        self.window.redraw()
        return {"ok": True, "node": _node_state(self.window, node_id)}

    def do_undo(self, _c):
        self.window.undo()
        return {"ok": True, "nodes": len(self.window.doc.order)}

    # -- whole-document ----------------------------------------------------

    def do_evaluate(self, _c):
        self.window.recompute()
        failed = {n: str(r) for n, r in self.window.results.items()
                  if isinstance(r, Exception)}
        return {"ok": True, "evaluated": len(self.window.doc.order),
                "errors": failed}

    def do_export(self, command):
        node_id = self._require(command, "id")
        path = self._require(command, "path")
        result = self.window.results.get(node_id)
        if not hasattr(result, "kind") or result.kind != "gcode":
            return {"ok": False, "error": f"{node_id} has not produced G-code"}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(result.data)
        return {"ok": True, "wrote": path,
                "lines": len(result.data.splitlines())}

    def do_save(self, command):
        path = command.get("path") or self.window.path
        if not path:
            return {"ok": False, "error": "this project has no path yet; "
                                          "pass one to save"}
        project_io.save(self.window.doc, path)
        self.window.path = path
        self.window._mark(False)
        return {"ok": True, "saved": path}

    def do_open(self, command):
        path = self._require(command, "path")
        self.window.doc = project_io.load(path)
        self.window.path = path
        self.window._mark(False)
        self.window.refresh_list()
        self.window.recompute()
        self.window.view.fit()
        return {"ok": True, "opened": path,
                "nodes": len(self.window.doc.order)}

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _require(command: dict, key: str) -> str:
        value = command.get(key)
        if not value:
            raise ValueError(f"'{key}' is required")
        return str(value)

    def run(self, command: dict) -> dict:
        """Dispatch one command. Called on the GUI thread, never elsewhere."""
        verb = str(command.get("command", "")).strip().lower()
        handler = getattr(self, f"do_{verb}", None) if verb.isidentifier() else None
        if handler is None:
            return {"ok": False, "error": f"unknown command: {verb or '(none)'}",
                    "known": self.verbs()}
        node_id = command.get("id")
        if node_id and node_id not in self.window.doc.nodes:
            return {"ok": False, "error": f"no such node: {node_id}"}
        return handler(command)

    def verbs(self) -> list[str]:
        return sorted(name[3:] for name in dir(self) if name.startswith("do_"))
