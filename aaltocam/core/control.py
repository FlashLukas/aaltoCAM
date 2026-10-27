"""A control channel for driving a running window from outside it.

Written so someone can keep clicking in the GUI while a script does the tedious
parts: setting twenty parameters, wiring a second side, exporting six jobs. It
is the same set of actions the window already offers, reachable over a socket.

Four things keep it from being a liability, and none of them is the transport:

**It is off unless switched on.** No port is opened at start-up. The window
says so in its title while it is listening, so it cannot be running quietly.

**It listens on the loopback address only**, and refuses any peer that is not
the local machine. Nothing outside this computer can reach it even if a
firewall rule were opened by accident.

**Every request carries a token** that is generated fresh each time the server
starts and written to a file in the user's own configuration directory. A
program that cannot read that file cannot issue a command.

**There is no "run this code" verb.** The vocabulary is fixed and small, and
every verb maps onto something the GUI can already do, so the worst a wrong
command can produce is a wrong toolpath -- visible on screen, and undoable with
Ctrl+Z like any other edit. That is the deliberate limit: a channel that could
execute arbitrary code would be a far more interesting thing to find open than
one that can only move a tool diameter.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import settings

#: Loopback only. Not configurable, deliberately.
HOST = "127.0.0.1"

#: Where the port and token are published for a local client to find.
HANDSHAKE = "control.json"

MAX_BODY = 1 << 20


def handshake_path() -> str:
    return os.path.join(os.path.dirname(settings.config_path()), HANDSHAKE)


def send(command: dict, timeout: float = 30.0) -> dict:
    """Send one command to whatever window is listening on this machine.

    Reads the handshake file to find the port and token, so a caller needs no
    configuration -- and, equally, a caller that cannot read that file cannot
    reach the window at all.

        from aaltocam.core.control import send
        send({"command": "list"})
        send({"command": "set", "id": "isolate_1", "params": {"passes": 3}})
    """
    import urllib.request

    try:
        with open(handshake_path(), encoding="utf-8") as fh:
            where = json.load(fh)
    except OSError:
        raise ConnectionError(
            "No window is offering control. Switch on Tools -> Allow local "
            f"control, which writes {handshake_path()}.") from None

    request = urllib.request.Request(
        f"http://{where['host']}:{where['port']}/", method="POST",
        data=json.dumps(command).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Aaltocam-Token": where["token"]})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.loads(reply.read())


class _Handler(BaseHTTPRequestHandler):
    server_version = "aaltocam"
    sys_version = ""

    def log_message(self, *_args):
        """Quiet. The window's status bar is the place for this."""

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Belt and braces: the socket is bound to loopback already, but a
        # second check costs nothing and documents the intent.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._reply(403, {"ok": False, "error": "not local"})
            return

        token = self.headers.get("X-Aaltocam-Token", "")
        if not secrets.compare_digest(token, self.server.token):
            self._reply(403, {"ok": False, "error": "bad or missing token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._reply(400, {"ok": False, "error": "empty or oversized body"})
            return

        try:
            command = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._reply(400, {"ok": False, "error": f"not JSON: {exc}"})
            return
        if not isinstance(command, dict):
            self._reply(400, {"ok": False, "error": "expected an object"})
            return

        try:
            result = self.server.dispatch(command)
        except Exception as exc:                      # never kill the server
            self._reply(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._reply(200, result)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, dispatch):
        super().__init__((HOST, 0), _Handler)
        self.dispatch = dispatch
        self.token = secrets.token_urlsafe(32)


class ControlServer:
    """The socket, its token, and the file that publishes both."""

    def __init__(self, dispatch):
        self._dispatch = dispatch
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    @property
    def token(self) -> str:
        return self._server.token if self._server else ""

    def start(self) -> int:
        if self._server is not None:
            return self.port
        self._server = _Server(self._dispatch)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="aaltocam-control", daemon=True)
        self._thread.start()
        self._publish()
        return self.port

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        try:
            os.remove(handshake_path())
        except OSError:
            pass

    def _publish(self):
        """Write where to reach it, for a client on this machine to pick up.

        Removed again on stop, so a stale file cannot send someone chasing a
        port that nothing is listening on.
        """
        path = handshake_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {"host": HOST, "port": self.port, "token": self.token,
                   "pid": os.getpid()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
