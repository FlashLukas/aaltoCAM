"""One version, in two places that have to agree.

`aaltocam.__version__` had been 0.12.1 for ten releases while pyproject.toml
climbed to 0.22.0. Nothing read it, which is exactly why nobody noticed; the
first thing to read it would have reported a version from months earlier.

The standalone build carries no package metadata to derive this from, so the
string stays written out and this test is what keeps it honest.
"""

from __future__ import annotations

import os
import tomllib

import aaltocam

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def declared_version() -> str:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_the_package_agrees_with_the_project_file():
    assert aaltocam.__version__ == declared_version()
