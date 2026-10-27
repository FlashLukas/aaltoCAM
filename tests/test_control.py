"""The local control channel: what it refuses, and what it is allowed to do.

The useful tests here are the negative ones. A channel that drives the window
is only reasonable because of what it cannot do, so the things worth pinning
down are that it is off until switched on, that it will not answer without the
token, that it publishes and then removes the file saying where it is, and
above all that no verb executes code. The day someone adds one of those, this
file should fail.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from aaltocam.core import control, settings


def post(port, body, token, timeout=10):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", method="POST",
        data=json.dumps(body).encode() if isinstance(body, (dict, list)) else body,
        headers={"X-Aaltocam-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A running server whose handshake lands in a temporary directory."""
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    srv = control.ControlServer(lambda command: {"ok": True, "echo": command})
    srv.start()
    yield srv
    srv.stop()


# --- it is off until asked for -------------------------------------------------

def test_the_channel_is_off_by_default():
    assert settings.DEFAULTS["control"] is False


def test_nothing_listens_until_start(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    srv = control.ControlServer(lambda c: {"ok": True})
    assert not srv.running
    assert srv.port == 0
    assert not os.path.exists(control.handshake_path())


# --- where it listens ----------------------------------------------------------

def test_it_binds_the_loopback_address_only(server):
    assert server._server.server_address[0] == "127.0.0.1"
    assert control.HOST == "127.0.0.1"


def test_the_handshake_says_where_to_reach_it(server):
    published = json.loads(open(control.handshake_path()).read())
    assert published["host"] == "127.0.0.1"
    assert published["port"] == server.port
    assert published["token"] == server.token
    assert published["pid"] == os.getpid()


def test_stopping_takes_the_handshake_away(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    srv = control.ControlServer(lambda c: {"ok": True})
    srv.start()
    assert os.path.exists(control.handshake_path())
    srv.stop()
    # A stale file would send a caller chasing a port nothing is listening on.
    assert not os.path.exists(control.handshake_path())
    assert not srv.running


# --- what it refuses -----------------------------------------------------------

def test_the_right_token_is_answered(server):
    status, body = post(server.port, {"command": "ping"}, server.token)
    assert status == 200 and body["ok"] is True


def test_a_wrong_token_is_refused(server):
    status, body = post(server.port, {"command": "ping"}, "not-the-token")
    assert status == 403
    assert body["ok"] is False


def test_a_missing_token_is_refused(server):
    status, _ = post(server.port, {"command": "ping"}, "")
    assert status == 403


def test_a_refusal_reaches_the_caller_every_time(server):
    """A refusal has to survive the connection closing, not usually survive it.

    Refusing returned without reading the request body, which left it in the
    receive buffer; closing a socket with data still in it makes Windows
    reset the connection, and the reset threw away the 403 that had just
    been written. The caller got ConnectionAbortedError and no reason at
    all -- the same failure as a reply that cannot be serialised, by a
    different route.

    It lost about three refusals in a hundred, which is why it showed up as
    this file failing now and then on a different test each time rather than
    as a bug. One request would never have caught it; this asks repeatedly.
    """
    for _ in range(200):
        status, body = post(server.port, {"command": "ping"}, "not-the-token")
        assert status == 403
        assert "token" in body["error"]


def test_the_token_is_long_enough_to_be_worth_having(server):
    assert len(server.token) >= 32


def test_a_fresh_token_each_time(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    tokens = set()
    for _ in range(3):
        srv = control.ControlServer(lambda c: {"ok": True})
        srv.start()
        tokens.add(srv.token)
        srv.stop()
    assert len(tokens) == 3


def test_rubbish_is_refused_not_crashed_on(server):
    status, body = post(server.port, b"this is not json", server.token)
    assert status == 400
    assert body["ok"] is False


def test_an_empty_body_is_refused(server):
    status, _ = post(server.port, b"", server.token)
    assert status == 400


def test_a_dispatch_that_raises_does_not_kill_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))

    def explode(_command):
        raise RuntimeError("boom")

    srv = control.ControlServer(explode)
    srv.start()
    try:
        status, body = post(srv.port, {"command": "ping"}, srv.token)
        assert status == 200 and body["ok"] is False and "boom" in body["error"]
        # Still answering afterwards.
        assert post(srv.port, {"command": "ping"}, srv.token)[0] == 200
    finally:
        srv.stop()


# --- the vocabulary ------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.gui import remote  # noqa: E402
from aaltocam.gui.app import MainWindow  # noqa: E402

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo", "demo.toml")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def driven(app, tmp_path, monkeypatch):
    """A window and the command set, without any socket in the way."""
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    window = MainWindow()
    yield window, remote.Commands(window)
    window._mark(False)
    window.close()


def test_no_verb_is_named_as_though_it_runs_code(driven):
    """Whole words, not substrings: 'evaluate' is recomputing the graph."""
    _, commands = driven
    forbidden = {"eval", "exec", "python", "code", "script", "shell",
                 "system", "import", "run", "call"}
    for verb in commands.verbs():
        words = set(verb.split("_"))
        assert not (words & forbidden), f"'{verb}' looks like it runs code"


def test_the_control_code_contains_no_way_to_execute_anything():
    """The property the naming test only gestures at.

    This is the whole argument for a channel that drives the window being a
    reasonable thing to have: every verb maps onto something the GUI already
    does, so the worst a wrong command produces is a wrong toolpath. The day
    an eval, an exec or a subprocess appears in either of these modules, that
    argument is gone and this should be what says so.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "aaltocam"
    patterns = (r"\beval\s*\(", r"\bexec\s*\(", r"\bcompile\s*\(",
                r"\b__import__\s*\(", r"\bsubprocess\b", r"\bos\.system\b",
                r"\bpickle\b", r"\bmarshal\b")
    for module in (root / "core" / "control.py", root / "gui" / "remote.py"):
        source = module.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not re.search(pattern, source), \
                f"{module.name} contains {pattern}"


def test_the_vocabulary_is_what_is_documented(driven):
    _, commands = driven
    assert set(commands.verbs()) == {
        "add", "connect", "delete", "evaluate", "export", "gcode", "get",
        "list", "open", "operations", "ping", "rename", "save", "set",
        "undo", "visible",
    }


def test_an_unknown_verb_is_refused_and_says_what_is_known(driven):
    _, commands = driven
    answer = commands.run({"command": "run_python", "code": "print(1)"})
    assert answer["ok"] is False
    assert "unknown command" in answer["error"]
    assert "set" in answer["known"]


def test_a_missing_node_is_refused_before_the_verb_runs(driven):
    _, commands = driven
    answer = commands.run({"command": "get", "id": "no_such_node_1"})
    assert answer["ok"] is False and "no such node" in answer["error"]


def test_an_unknown_parameter_is_refused(driven):
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    answer = commands.run({"command": "set", "id": "isolate_1",
                           "params": {"not_a_parameter": 1}})
    assert answer["ok"] is False and "no parameter" in answer["error"]


# --- what it can do ------------------------------------------------------------

def test_it_can_read_the_graph(driven):
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    listed = commands.run({"command": "list"})
    assert listed["ok"] is True
    assert {n["op"] for n in listed["nodes"]} >= {"load_gerber", "isolate", "cnc_job"}


def test_setting_a_parameter_reaches_the_document_and_the_gcode(driven):
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    before = commands.run({"command": "gcode", "id": "cnc_job_1"})["lines"]

    answer = commands.run({"command": "set", "id": "isolate_1",
                           "params": {"passes": 4}})
    assert answer["ok"] is True
    assert window.doc.nodes["isolate_1"].params["passes"] == 4
    after = commands.run({"command": "gcode", "id": "cnc_job_1"})["lines"]
    assert after > before          # more passes really is more cutting


def test_every_change_is_undoable_by_hand(driven):
    """A command you did not want is Ctrl+Z away, like any other edit."""
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    original = window.doc.nodes["isolate_1"].params["passes"]

    commands.run({"command": "set", "id": "isolate_1", "params": {"passes": 7}})
    window.undo()
    assert window.doc.nodes["isolate_1"].params["passes"] == original


def test_it_can_add_and_delete(driven):
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    count = len(window.doc.order)

    added = commands.run({"command": "add", "op": "alignment_holes",
                          "inputs": ["load_gerber_1"], "name": "Pins"})
    assert added["ok"] is True and added["node"]["name"] == "Pins"
    assert len(window.doc.order) == count + 1

    commands.run({"command": "delete", "id": added["node"]["id"]})
    assert len(window.doc.order) == count


def test_it_can_export_gcode(driven, tmp_path):
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    out = tmp_path / "job.nc"
    answer = commands.run({"command": "export", "id": "cnc_job_1",
                           "path": str(out)})
    assert answer["ok"] is True
    assert out.read_text(encoding="utf-8").strip().endswith("M2")


def test_exporting_something_that_is_not_a_job_is_refused(driven, tmp_path):
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    answer = commands.run({"command": "export", "id": "load_gerber_1",
                           "path": str(tmp_path / "nope.nc")})
    assert answer["ok"] is False


def test_operations_lists_what_can_be_added(driven):
    _, commands = driven
    answer = commands.run({"command": "operations"})
    ops_by_name = {o["op"] for o in answer["operations"]}
    assert {"isolate", "cnc_job", "mill_slots", "alignment_holes"} <= ops_by_name


# --- replies that have to survive the wire -------------------------------------

def test_geometry_in_a_result_does_not_take_the_reply_with_it(driven):
    """The bug this guards: a rest-machining job's meta carries its tool
    groups as lists of Shapely LineStrings. A filter that tested the container
    and not its contents let them through as 'a list', json.dumps raised after
    the handler had committed to replying, and the caller saw the connection
    drop with nothing to go on."""
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    node = window.doc.add("clear_copper", ["load_gerber_1"], name="Clearing")
    window.refresh_list()
    window.recompute()

    answer = commands.run({"command": "get", "id": node.id})
    json.dumps(answer)                              # must not raise
    groups = answer["node"]["meta"].get("groups")
    if groups:
        assert "LineString" in json.dumps(groups)   # described, not carried


def test_every_reply_from_a_real_project_serialises(driven):
    window, commands = driven
    commands.run({"command": "open", "path": DEMO})
    json.dumps(commands.run({"command": "list"}))
    for node_id in list(window.doc.order):
        json.dumps(commands.run({"command": "get", "id": node_id}))


def test_list_leaves_the_parameters_out(driven):
    """A region masking rest machining holds hundreds of polygons; a listing
    carrying all of them is megabytes the caller almost never wanted."""
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    listed = commands.run({"command": "list"})
    for node in listed["nodes"]:
        assert "params" not in node
        assert isinstance(node["param_names"], list)


def test_get_still_gives_the_parameters(driven):
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    node = commands.run({"command": "get", "id": "isolate_1"})["node"]
    assert node["params"]["passes"] >= 1


def test_a_listing_stays_small(driven):
    _, commands = driven
    commands.run({"command": "open", "path": DEMO})
    assert len(json.dumps(commands.run({"command": "list"}))) < 100_000


@pytest.mark.parametrize("value, expected", [
    (None, None), (True, True), (3, 3), ("x", "x"), (1.5, 1.5),
    ([1, [2, [3]]], [1, [2, [3]]]),
    ({"a": 1}, {"a": 1}),
])
def test_plain_values_pass_through_unchanged(value, expected):
    assert remote._jsonable(value) == expected


def test_an_unknown_type_is_described_rather_than_carried():
    assert remote._jsonable(object()) == "<object>"
    assert remote._jsonable({"k": object()}) == {"k": "<object>"}


def test_a_long_list_is_truncated_and_says_so():
    out = remote._jsonable(list(range(remote.MAX_ITEMS + 500)))
    assert len(out) == remote.MAX_ITEMS + 1
    assert "500 more" in out[-1]


def test_deep_nesting_stops_rather_than_recursing_forever():
    deep = value = []
    for _ in range(50):
        inner = []
        value.append(inner)
        value = inner
    json.dumps(remote._jsonable(deep))              # must not blow the stack


def test_infinities_are_not_emitted_as_bare_json(driven):
    """NaN and inf are not JSON, however willingly Python writes them."""
    assert remote._jsonable(float("inf")) == "inf"
    assert remote._jsonable(float("nan")) == "nan"


def test_an_unserialisable_reply_answers_rather_than_dropping(tmp_path, monkeypatch):
    """Even with the filter in place, the transport must not die silently."""
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    srv = control.ControlServer(lambda _c: {"ok": True, "thing": object()})
    srv.start()
    try:
        status, body = post(srv.port, {"command": "ping"}, srv.token)
        assert status == 500
        assert body["ok"] is False
        assert "could not be serialised" in body["error"]
        # And it is still answering afterwards.
        assert post(srv.port, {"command": "ping"}, srv.token)[0] == 500
    finally:
        srv.stop()


# --- the bridge ----------------------------------------------------------------

def test_a_command_from_another_thread_waits_for_the_window(driven):
    """The socket answers on a worker thread; the document is only ever
    touched on the GUI thread, which is what this hand-off exists for."""
    _, commands = driven
    bridge = remote.Bridge()
    answers = {}

    def caller():
        answers["reply"] = bridge.submit({"command": "ping"})

    thread = threading.Thread(target=caller)
    thread.start()
    for _ in range(200):                       # stand in for the GUI timer
        bridge.pump(commands.run)
        if "reply" in answers:
            break
    thread.join(timeout=5)
    assert answers["reply"]["ok"] is True


def test_a_window_that_never_answers_times_out_rather_than_hanging():
    bridge = remote.Bridge()
    bridge.__class__.TIMEOUT_S = 0.2
    original, remote.TIMEOUT_S = remote.TIMEOUT_S, 0.2
    try:
        answer = bridge.submit({"command": "ping"})
    finally:
        remote.TIMEOUT_S = original
    assert answer["ok"] is False and answer["timeout"] is True
    assert "may still be running" in answer["error"]
