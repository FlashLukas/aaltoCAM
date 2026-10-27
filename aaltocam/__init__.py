"""aaltocam - parametric CAM for PCB isolation milling."""

#: Kept in step with pyproject.toml by tests/test_version.py.
#:
#: It is written out rather than read from the package metadata because the
#: standalone build has no metadata to read -- PyInstaller bundles the
#: modules, not a dist-info directory -- and a version that raises inside the
#: released executable is worse than one that has to be bumped by hand. It
#: had in fact drifted ten releases behind before the test existed.
__version__ = "0.22.0"
