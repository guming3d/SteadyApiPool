"""``python -m pool_tui``: the terminal UI for SteadyApiPool."""

from __future__ import annotations

import argparse
import os
import sys

from .app import PoolManagerApp
from .backend import resolve
from .client import ApiClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pool_tui", description=__doc__)
    parser.add_argument(
        "--api",
        default=os.environ.get("POOL_TUI_API"),
        help="Backend base URL, e.g. http://127.0.0.1:8000 (default: reuse or start a local one)",
    )
    parser.add_argument("--no-start", action="store_true", help="Never start a backend")
    args = parser.parse_args(argv)
    try:
        url, backend = resolve(args.api, start=not args.no_start)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        PoolManagerApp(ApiClient(url)).run()
    finally:
        if backend is not None:
            backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
