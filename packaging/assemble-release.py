"""Assemble the Windows release folder and zip it.

`aaltocam.exe` is a frozen bundle carrying Qt, GEOS and a dozen Python
libraries inside it. Publishing it is redistribution, so their licence texts
travel with it in `third-party-licenses/`.

Two of those texts are not in any wheel and are kept in `packaging/third-party`
instead: Qt's LGPL-3.0, because the PySide6 wheel ships only a reference to
Qt's *commercial* licence, and the GPL-3.0 that the LGPL incorporates by
reference. gerbonara ships no licence file at all, so it gets a copy of the
project's own Apache-2.0 text.

Run it after packaging/install-aaltocam.ps1 has built with -BuildExe:

    python packaging/assemble-release.py --root ~/aaltocam-work --out dist-release
"""

import argparse
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Runtime packages frozen into the executable. Build-only tooling (pip,
# setuptools, PyInstaller and its helpers) is not shipped and is not listed.
RUNTIME = [
    "pyside6_essentials", "shiboken6", "shapely", "numpy", "gerbonara",
    "rtree", "click", "quart", "flask", "jinja2", "markupsafe", "werkzeug",
    "itsdangerous", "blinker", "aiofiles", "h11", "h2", "hpack", "hyperframe",
    "hypercorn", "priority", "wsproto",
]

QT_TEXTS = ("Qt-LGPL-3.0.txt", "Qt-GPL-3.0.txt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path,
                    help="the -Root the installer built into")
    ap.add_argument("--out", required=True, type=Path,
                    help="where to write the release folder and zip")
    ap.add_argument("--version", default=None,
                    help="defaults to the version in pyproject.toml")
    args = ap.parse_args()

    dist = args.root / "dist" / "aaltocam"
    site = args.root / ".venv" / "Lib" / "site-packages"
    for path, what in ((dist, "build"), (site, "virtual environment")):
        if not path.is_dir():
            print(f"error: no {what} at {path}", file=sys.stderr)
            return 1

    version = args.version
    if not version:
        with (REPO / "pyproject.toml").open("rb") as fh:
            version = tomllib.load(fh)["project"]["version"]

    name = f"aaltocam-{version}-win64"
    staging = args.out / name
    if staging.exists():
        shutil.rmtree(staging)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"copying build -> {staging}")
    shutil.copytree(dist, staging)

    for fname in ("LICENSE", "NOTICE", "ATTRIBUTION.md", "README.md"):
        shutil.copy2(REPO / fname, staging / fname)

    tpl = staging / "third-party-licenses"
    tpl.mkdir()

    found, missing = {}, []
    for pkg in RUNTIME:
        hits = sorted(site.glob(f"{pkg}-*.dist-info"))
        if not hits:
            missing.append(pkg)
            continue
        files = [f for f in hits[0].rglob("*") if f.is_file()
                 and f.name.upper().startswith(("LICEN", "COPYING", "NOTICE"))]
        if not files:
            missing.append(pkg)
            continue
        target = tpl / pkg
        target.mkdir()
        for f in files:
            shutil.copy2(f, target / f.name)
        found[pkg] = sorted(f.name for f in files)

    # Qt: the wheel carries only the commercial licence reference.
    qt = tpl / "pyside6_essentials"
    qt.mkdir(exist_ok=True)
    for text in QT_TEXTS:
        src = REPO / "packaging" / "third-party" / text
        if not src.exists():
            print(f"error: missing {src}", file=sys.stderr)
            return 1
        shutil.copy2(src, qt / text)

    # gerbonara is Apache-2.0 but ships no text.
    gerb = tpl / "gerbonara"
    gerb.mkdir(exist_ok=True)
    shutil.copy2(REPO / "LICENSE", gerb / "Apache-2.0.txt")
    missing = [m for m in missing if m != "gerbonara"]

    index = [
        "# Third-party licences",
        "",
        "`aaltocam.exe` is a frozen bundle. These are the licences of the",
        "components inside it. aaltoCAM's own licence is `LICENSE` in the",
        "folder above, and `ATTRIBUTION.md` explains what each component does.",
        "",
        "## Qt, via PySide6 and shiboken6",
        "",
        "Used under the **LGPL-3.0**. The PySide6 wheel ships only a reference",
        "to Qt's commercial licence, so `Qt-LGPL-3.0.txt` and the",
        "`Qt-GPL-3.0.txt` it incorporates by reference are included here.",
        "",
        "The Qt DLLs are separate files in this folder rather than welded into",
        "the executable. That is the PyInstaller `onedir` layout, and it is",
        "intended to leave relinking against another Qt build possible.",
        "",
        "## GEOS, via Shapely",
        "",
        "`shapely/LICENSE_GEOS` — LGPL-2.1. Shapely itself is BSD-3-Clause.",
        "",
        "## gerbonara",
        "",
        "Apache-2.0. Its wheel carries no licence file, so the canonical text",
        "is included as `gerbonara/Apache-2.0.txt`.",
        "",
        "## Everything else",
        "",
    ]
    index += [f"- `{pkg}/` — {', '.join(found[pkg])}" for pkg in sorted(found)]
    index.append("")
    (tpl / "INDEX.md").write_text("\n".join(index), encoding="utf-8")

    archive = args.out / f"{name}.zip"
    archive.unlink(missing_ok=True)
    print(f"zipping -> {archive}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(staging.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(staging.parent))

    files = [f for f in staging.rglob("*") if f.is_file()]
    print()
    print(f"version  : {version}")
    print(f"folder   : {staging}")
    print(f"           {len(files)} files, {sum(f.stat().st_size for f in files) / 1e6:.0f} MB")
    print(f"zip      : {archive} ({archive.stat().st_size / 1e6:.0f} MB)")
    print(f"licences : {len(found) + 1} packages")

    if missing:
        # Shipping a binary without one of its licences is the failure that
        # matters here, so refuse rather than warn.
        print(f"error: no licence text found for: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
