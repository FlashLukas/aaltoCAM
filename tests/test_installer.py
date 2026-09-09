"""Guard the Windows installer against three bugs that kept coming back.

The installer used to live only beside the release tarballs, so every release
rewrote it and reintroduced the same PowerShell 5.1 failures. It now lives in
the source tree, which is only half the fix: this file is the other half. Each
test below is a bug that has actually shipped, written so it fails on the
pattern rather than on the wording, and each failure message says what the
symptom looked like on the machine.

The target shell is Windows PowerShell 5.1 -- pwsh is not installed there --
so nothing in the installer may depend on PowerShell 7 behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parent.parent / "packaging"
INSTALLER = PACKAGING / "install-aaltocam.ps1"
RELEASE = PACKAGING / "release-aaltocam.ps1"


@pytest.fixture(scope="module")
def script() -> str:
    assert INSTALLER.is_file(), (
        f"{INSTALLER} is missing. The installer belongs in the source tree; a "
        "copy kept anywhere else gets rewritten from memory at release time, "
        "which is how these bugs shipped three times running."
    )
    return INSTALLER.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop block comments and line comments, so prose about a bug is not
    mistaken for the bug."""
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


# --- bug 1: the Python probe -----------------------------------------------

def test_no_quoted_inline_python(script):
    """A -c snippet containing double quotes is destroyed on 5.1.

    Windows PowerShell 5.1 builds the command line for a native executable
    without escaping embedded double quotes, so

        & $py -c 'import sys; print("%d.%d" % sys.version_info[:2])'

    reaches Python as `import sys; print(` and raises SyntaxError. Every
    interpreter then looks broken, the script decides none exists and tries to
    winget-install a Python that is already there, dying on that instead --
    which hides the real cause completely.
    """
    body = _strip_comments(script)
    offenders = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"-c\s+'[^']*\"", line)
    ]
    assert not offenders, (
        "A -c snippet carries embedded double quotes, which PowerShell 5.1 "
        "strips before Python sees them:\n  "
        + "\n  ".join(offenders)
        + "\nUse a quote-free snippet, or -V plus a regex."
    )


def test_version_probe_uses_dash_v(script):
    """The version check must ask with -V, which carries nothing to mangle."""
    probe = re.search(r"function\s+Get-PythonVersion\b.*?\n}", script,
                      flags=re.DOTALL)
    assert probe, "Get-PythonVersion is gone; something replaced the probe."
    assert re.search(r"&\s*\$\w+\s+-V\b", probe.group(0)), (
        "Get-PythonVersion no longer asks with -V. -V prints 'Python 3.13.15' "
        "on anything 3.4 or newer and survives 5.1's quoting intact; -c does "
        "not."
    )


def test_store_stub_still_rejected(script):
    """The Microsoft Store placeholder must not be mistaken for Python."""
    assert "WindowsApps" in script, (
        "The *WindowsApps* filter is gone. The Store stub at "
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe sits on PATH, is not "
        "an interpreter, and opens the Store when run."
    )


# --- bug 2: native stderr under 'Stop' --------------------------------------

def test_native_calls_are_guarded(script):
    """pip, PyInstaller, winget, tar and venv must go through Invoke-Native.

    Under $ErrorActionPreference = 'Stop', 5.1 turns every stderr line from a
    native program into a terminating NativeCommandError as soon as output is
    redirected. pip and PyInstaller both log progress to stderr, so piping the
    installer to a log file killed the build partway.
    """
    assert "function Invoke-Native" in script, (
        "Invoke-Native is gone. Without it, redirecting this script's output "
        "to a file aborts the build the first time pip writes to stderr."
    )

    body = _strip_comments(script)
    # Every line that runs one of these must be inside an Invoke-Native block:
    # either on the same line, or in the two lines above it.
    lines = body.splitlines()
    risky = re.compile(r"&\s*[^|]*?(?:\bwinget\b|\btar\b|-m\s+pip\b|"
                       r"-m\s+PyInstaller\b|-m\s+venv\b)")
    offenders = []
    for index, line in enumerate(lines):
        if not risky.search(line):
            continue
        window = "\n".join(lines[max(0, index - 3):index + 1])
        if "Invoke-Native" not in window:
            offenders.append(f"line {index + 1}: {line.strip()}")
    assert not offenders, (
        "These native calls are not wrapped in Invoke-Native, so a redirected "
        "run dies on their first line of stderr:\n  " + "\n  ".join(offenders)
    )


def test_invoke_native_restores_the_preference(script):
    """The relaxed preference must not leak past the call it was relaxed for."""
    block = re.search(r"function\s+Invoke-Native\b.*?\n}", script, flags=re.DOTALL)
    assert block, "Invoke-Native is gone."
    text = block.group(0)
    assert "finally" in text and "$ErrorActionPreference = $previous" in text, (
        "Invoke-Native must restore $ErrorActionPreference in a finally block, "
        "or one guarded call leaves the whole rest of the script running "
        "without 'Stop'."
    )
    assert "$LASTEXITCODE" in text, (
        "Invoke-Native must still fail on a non-zero exit code -- that is the "
        "signal worth reacting to once stderr no longer is."
    )


def test_pyinstaller_failure_is_not_fatal(script):
    """A failed exe build must warn, not throw away a good venv install."""
    tail = script[script.find("if ($BuildExe)"):]
    assert "try {" in tail and "catch" in tail, (
        "The PyInstaller call is no longer wrapped in try/catch. A failed "
        "standalone build should leave the working virtual environment in "
        "place and say so, not abort the installer."
    )


# --- bug 3: the stale unpacked tree -----------------------------------------

def test_expand_source_clears_before_extracting(script):
    """tar overwrites but never deletes, and the leftovers ship.

    A module the new version removed survives in the unpacked tree, setuptools'
    package discovery sweeps it into the wheel, and the build is subtly wrong
    with no error anywhere -- the most dangerous of the three.
    """
    block = re.search(r"function\s+Expand-Source\b.*?\n}", script, flags=re.DOTALL)
    assert block, "Expand-Source is gone."
    text = block.group(0)
    remove_at = text.find("Remove-Item")
    extract_at = text.find("tar -xzf")
    assert remove_at != -1, (
        "Expand-Source no longer clears the destination. Upgrading in an "
        "existing -Root then ships stale modules inside the wheel, silently."
    )
    assert extract_at != -1, "Expand-Source no longer runs tar."
    assert remove_at < extract_at, (
        "Expand-Source removes the old tree after extracting, which deletes "
        "the new one. Clear first, then extract."
    )


# --- the release wrapper ----------------------------------------------------

def test_release_script_ships_too():
    assert RELEASE.is_file(), (
        f"{RELEASE} is missing. It belongs beside the installer for the same "
        "reason: kept anywhere else, it gets rewritten."
    )


def test_release_script_has_no_repair_hack():
    """The workaround for bug 1 is dead code once the installer is correct."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "aaltocam-release" not in text, (
        "release-aaltocam.ps1 still patches a copy of the installer under "
        "%TEMP%. That workaround existed because the installer was rewritten "
        "each release; it now lives in the tree and is tested, so the repair "
        "step should be gone."
    )


def test_release_uses_the_installer_from_the_tarball():
    """The installer that runs must be the one that shipped with that version."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "packaging" in text, (
        "release-aaltocam.ps1 should run packaging/install-aaltocam.ps1 from the "
        "unpacked tarball, so the installer and the source it installs are "
        "always the same version."
    )
