#!/usr/bin/env python3
"""Builds the release package: the app with its browser UI already built, so running it needs
Python only (no Node.js, no git):

    cd frontend && npm ci && npm run build && cd ..
    python scripts/package-release.py [--version v0.2.0] [--out dist]

The zip holds backend/ (with pool_manager/static and the gateway template), the start scripts
and the doctor, under one SteadyApiPool-<version>/ folder. Tests, virtual environments, local
settings and sign-ins are left out. .github/workflows/release.yml runs this for every v* tag.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_INDEX = ROOT / "backend" / "pool_manager" / "static" / "index.html"

# (folder, files and folders inside it to ship)
CONTENTS = [
    ("backend", ["pool_manager", "requirements.txt", "pyproject.toml", ".env.example"]),
    (
        "scripts",
        ["start-local.ps1", "start-local.sh", "start-local.cmd", "doctor.py", "benchmark.py"],
    ),
    (".", ["README.md", "README.zh-CN.md", "LICENSE"]),
]
SKIP_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests"}
SKIP_FILES = re.compile(r"(\.py[cod]$|^\.env$|^\.pool-manager.*\.(json|bin)$|\.log$)")
# Line endings the scripts need whatever the checkout used (see .gitattributes).
LF = {".sh", ".py"}
CRLF = {".cmd", ".ps1"}


def _version() -> str:
    text = (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else "0.0.0"


def _files() -> Iterator[Path]:
    for folder, names in CONTENTS:
        for name in names:
            path = ROOT / folder / name
            if path.is_file():
                yield path
            elif path.is_dir():
                for item in sorted(path.rglob("*")):
                    parts = set(item.relative_to(ROOT).parts)
                    if (
                        item.is_file()
                        and not parts & SKIP_DIRS
                        and not SKIP_FILES.search(item.name)
                    ):
                        yield item
            elif name != "LICENSE":
                raise SystemExit(f"error: {path.relative_to(ROOT)} is missing")


def _content(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix in LF | CRLF:
        data = data.replace(b"\r\n", b"\n")
        if path.suffix in CRLF:
            data = data.replace(b"\n", b"\r\n")
    return data


def build(version: str, out: Path) -> Path:
    if not STATIC_INDEX.exists():
        raise SystemExit(
            "error: the browser UI isn't built; run 'npm ci && npm run build' in frontend/ first"
        )
    name = f"SteadyApiPool-{version.removeprefix('v')}"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{name}.zip"
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _files():
            info = zipfile.ZipInfo(f"{name}/{path.relative_to(ROOT).as_posix()}", stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            # unzip on macOS and Linux keeps these modes, so the scripts run directly.
            mode = (
                0o755 if path.suffix in {".sh", ".py"} and path.parent.name == "scripts" else 0o644
            )
            info.external_attr = (0o100000 | mode) << 16
            # Unix, wherever it is built: unzip ignores the mode of entries marked as made on
            # Windows (ZipInfo's default there), and the scripts would not be executable.
            info.create_system = 3
            archive.writestr(info, _content(path))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", default=_version(), help="defaults to backend/pyproject.toml")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    target = build(args.version, args.out)
    with zipfile.ZipFile(target) as archive:
        count = len(archive.namelist())
    print(f"{target} ({count} files, {target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
