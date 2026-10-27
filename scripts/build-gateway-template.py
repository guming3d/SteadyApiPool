#!/usr/bin/env python3
"""Compiles the gateway templates in infra/gateway/ (Container Apps and AKS) into the ARM
templates the app deploys (backend/pool_manager/templates/). Needs the Azure CLI with Bicep.

    python scripts/build-gateway-template.py           # rewrite the JSON
    python scripts/build-gateway-template.py --check   # fail when one is out of date (CI)

The check ignores the generator metadata (Bicep version, template hash), so a newer Bicep
that compiles to the same template passes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ["litellm-aca", "litellm-aks"]


def _source(name: str) -> Path:
    return ROOT / "infra" / "gateway" / f"{name}.bicep"


def _target(name: str) -> Path:
    return ROOT / "backend" / "pool_manager" / "templates" / f"{name}.json"


def _compile(source: Path) -> str:
    az = shutil.which("az")
    if not az:
        raise SystemExit("error: the Azure CLI (az) is needed to compile Bicep")
    result = subprocess.run(
        [az, "bicep", "build", "--file", str(source), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"error: az bicep build failed:\n{result.stderr}")
    return result.stdout


def _content(text: str) -> Any:
    template = json.loads(text)
    template.get("metadata", {}).pop("_generator", None)
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check that the JSON is current")
    args = parser.parse_args()
    stale = 0
    for name in TEMPLATES:
        compiled = _compile(_source(name))
        target = _target(name)
        shown = target.relative_to(ROOT)
        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else "{}"
            if _content(current) != _content(compiled):
                print(
                    f"error: {shown} is out of date; run python scripts/build-gateway-template.py",
                    file=sys.stderr,
                )
                stale += 1
            else:
                print(f"{shown} is current")
            continue
        target.write_text(compiled, encoding="utf-8", newline="\n")
        print(f"wrote {shown}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
