#!/usr/bin/env python3
"""Compiles infra/gateway/litellm-aca.bicep into the ARM template the app deploys
(backend/pool_manager/templates/litellm-aca.json). Needs the Azure CLI with Bicep.

    python scripts/build-gateway-template.py           # rewrite the JSON
    python scripts/build-gateway-template.py --check   # fail when it is out of date (CI)

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
SOURCE = ROOT / "infra" / "gateway" / "litellm-aca.bicep"
TARGET = ROOT / "backend" / "pool_manager" / "templates" / "litellm-aca.json"


def _compile() -> str:
    az = shutil.which("az")
    if not az:
        raise SystemExit("error: the Azure CLI (az) is needed to compile Bicep")
    result = subprocess.run(
        [az, "bicep", "build", "--file", str(SOURCE), "--stdout"],
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
    compiled = _compile()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else "{}"
        if _content(current) != _content(compiled):
            print(
                f"error: {TARGET.relative_to(ROOT)} is out of date; "
                "run python scripts/build-gateway-template.py",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET.relative_to(ROOT)} is current")
        return 0
    TARGET.write_text(compiled, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
