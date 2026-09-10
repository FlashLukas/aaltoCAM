"""The theme setting, and the file it lives in.

Two things matter here beyond "the colours change". A settings file that has
been hand-edited into nonsense must never stop the program starting -- the worst
a bad theme can cost is ugly, and an application that will not open costs the
job. And the choice is applied at start-up rather than live, so writing it down
must not restyle the running window: saying "restart" and then half-restyling
would be worse than either.

The board-view colours are module globals, so every test here puts them back.
"""

from __future__ import annotations

import os

import pytest

from aaltocam.core import settings

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.gui import app as gui  # noqa: E402
from aaltocam.gui import canvas  # noqa: E402


@pytest.fixture(autouse=True)
def restore_theme():
    yield
    canvas.use_theme("dark")


@pytest.fixture
def config(tmp_path):
    return str(tmp_path / "settings.toml")


# --- the file -----------------------------------------------------------------

def test_a_missing_file_gives_the_defaults(config):
    assert settings.load(config) == settings.DEFAULTS


def test_a_saved_theme_comes_back(config):
    settings.save({"theme": "light"}, config)
    assert settings.load(config)["theme"] == "light"


def test_set_theme_leaves_a_readable_file(config):
    settings.set_theme("light", config)
    text = open(config, encoding="utf-8").read()
    assert 'theme = "light"' in text
    assert text.startswith("#")          # says what wrote it


def test_a_corrupt_file_falls_back_rather_than_raising(config):
    with open(config, "w", encoding="utf-8") as fh:
        fh.write("this is not = = toml [[[\n")
    assert settings.load(config) == settings.DEFAULTS


def test_an_unknown_theme_falls_back(config):
    settings.save({"theme": "chartreuse"}, config)
    assert settings.load(config)["theme"] == "dark"


def test_unknown_keys_are_ignored(config):
    settings.save({"theme": "light", "favourite_biscuit": "hobnob"}, config)
    loaded = settings.load(config)
    assert loaded["theme"] == "light"
    assert "favourite_biscuit" not in loaded


def test_setting_an_unknown_theme_is_refused(config):
    with pytest.raises(ValueError, match="unknown theme"):
        settings.set_theme("chartreuse", config)


def test_the_settings_sit_beside_the_tool_library():
    from aaltocam.core import tools as toollib
    assert (os.path.dirname(settings.config_path())
            == os.path.dirname(toollib.config_path()))


# --- the colours --------------------------------------------------------------

def test_the_two_palettes_cover_the_same_names():
    assert set(canvas.DARK) == set(canvas.LIGHT)


def test_switching_rebinds_the_drawing_colours():
    canvas.use_theme("dark")
    dark_bg = canvas.BACKGROUND
    canvas.use_theme("light")
    assert canvas.BACKGROUND != dark_bg
    assert canvas.BACKGROUND.lightness() > dark_bg.lightness()


def test_the_light_board_is_lighter_than_its_ink():
    canvas.use_theme("light")
    assert canvas.BACKGROUND.lightness() > canvas.ORIGIN.lightness()


def test_the_dark_board_is_darker_than_its_ink():
    canvas.use_theme("dark")
    assert canvas.BACKGROUND.lightness() < canvas.ORIGIN.lightness()


def test_an_unknown_name_is_treated_as_dark():
    canvas.use_theme("chartreuse")
    assert canvas.BACKGROUND == canvas.DARK["BACKGROUND"]


def test_the_light_palette_defines_bright_text():
    """Left to Fusion it comes out near-white, and the palette headings vanish."""
    light = gui.light_palette()
    assert (light.color(QPalette.BrightText).lightness()
            < light.color(QPalette.Window).lightness())


# --- applying it --------------------------------------------------------------

def test_apply_theme_sets_both_the_window_and_the_board():
    app = QApplication.instance() or QApplication([])
    gui.apply_theme(app, "light")
    assert canvas.BACKGROUND == canvas.LIGHT["BACKGROUND"]
    assert (app.palette().color(QPalette.Window).lightness()
            > app.palette().color(QPalette.WindowText).lightness())
    gui.apply_theme(app, "dark")
    assert canvas.BACKGROUND == canvas.DARK["BACKGROUND"]


def test_choosing_a_theme_records_it_without_restyling(tmp_path, monkeypatch):
    """It takes effect next start. It must not half-apply now."""
    config = str(tmp_path / "settings.toml")
    monkeypatch.setattr(settings, "config_path", lambda: config)

    app = QApplication.instance() or QApplication([])
    gui.apply_theme(app, "dark")
    window = gui.MainWindow()
    try:
        before = canvas.BACKGROUND
        window.choose_theme("light")
        assert settings.load(config)["theme"] == "light"
        assert canvas.BACKGROUND == before          # the running view is untouched
    finally:
        window._mark(False)
        window.close()
