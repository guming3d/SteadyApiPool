"""Reports the live Azure journeys (.github/workflows/e2e-azure.yml) as GitHub issues, so a
scheduled run that fails emails the people the issue is assigned to (GitHub sends it to their
notification address). Run in the workflow, with the gh CLI and GH_TOKEN (issues: write).

    python scripts/e2e-report.py failed --journey aca --log journey.log --run-url <url>
    python scripts/e2e-report.py passed --journey aca --run-url <url>

``failed`` opens "Live E2E failing: <journey>" (label ``e2e-failure``, assigned to
``--assignee``, default the repository's owner) with the steps that failed, their errors and the
end of the log, or comments on that issue if it is already open. ``passed`` closes it with a
note. Subscription and other IDs, and anything that looks like a key, are redacted: the
repository is public. ``--dry-run`` prints instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any

LABEL = "e2e-failure"
_STEP = re.compile(
    r"::(test_[A-Za-z0-9_]+)(?:\s|\S)*?\b(FAILED|ERROR)\b|\b(FAILED|ERROR) \S*::(test_\w+)"
)
_ERROR = re.compile(r"^E\s+(?:harness\.Failure|AssertionError|\w+Error)\b.*")
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|[0-9a-f]{32,})\b", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
MAX_ERRORS = 8
MAX_ERROR_CHARS = 600
TAIL_LINES = 25


def redact(text: str) -> str:
    """No IDs or key-like strings in a public issue."""
    return _SECRET.sub("<redacted>", _GUID.sub("<id>", text))


def summarize(log: str) -> tuple[list[str], list[str], list[str]]:
    """The failed steps, their errors (the first lines pytest marks with E) and the log's end."""
    lines = [_ANSI.sub("", line).rstrip() for line in log.splitlines()]
    steps: list[str] = []
    for line in lines:
        match = _STEP.search(line)
        if match:
            name = match.group(1) or match.group(4)
            if name and name not in steps:
                steps.append(name)
    errors: list[str] = []
    for line in lines:
        if _ERROR.match(line):
            text = line[: MAX_ERROR_CHARS - 1] + "…" if len(line) > MAX_ERROR_CHARS else line
            if text not in errors:
                errors.append(text)
        if len(errors) >= MAX_ERRORS:
            break
    tail = [line for line in lines if line.strip()][-TAIL_LINES:]
    return steps, errors, tail


def body(journey: str, run_url: str, log: str) -> str:
    steps, errors, tail = summarize(log)
    parts = [
        f"The live **{journey}** journey failed in a scheduled run: {run_url}",
        "",
        "**Failed steps:** "
        + (", ".join(f"`{step}`" for step in steps) or "none named (see the log)"),
        "",
    ]
    if errors:
        parts += ["**Errors:**", "```", *errors, "```", ""]
    parts += [
        "<details><summary>End of the log</summary>",
        "",
        "```",
        *tail,
        "```",
        "</details>",
    ]
    parts += ["", "This issue closes when the journey passes again (docs/testing.md)."]
    return redact("\n".join(parts))


def gh(*args: str) -> Any:
    executable = shutil.which("gh")
    if executable is None:
        raise SystemExit("The GitHub CLI (gh) is required")
    result = subprocess.run([executable, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip().startswith(("[", "{")) else None


def _open_issue(repo: str, title: str) -> int | None:
    found = gh(
        "issue", "list", "--repo", repo, "--label", LABEL, "--state", "open",
        "--json", "number,title", "--limit", "50",
    )  # fmt: skip
    return next((int(item["number"]) for item in found or [] if item["title"] == title), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("outcome", choices=["failed", "passed"])
    parser.add_argument("--journey", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--log", help="the journey's output (failed)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--assignee", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    title = f"Live E2E failing: {args.journey}"
    if args.outcome == "failed":
        log = ""
        if args.log and os.path.exists(args.log):
            with open(args.log, encoding="utf-8", errors="replace") as handle:
                log = handle.read()
        text = body(args.journey, args.run_url, log)
        if args.dry_run:
            print(f"# {title}\n\n{text}")
            return 0
        gh("label", "create", LABEL, "--repo", args.repo, "--color", "B60205", "--force",
           "--description", "A scheduled live Azure journey failed")  # fmt: skip
        number = _open_issue(args.repo, title)
        if number is None:
            gh("issue", "create", "--repo", args.repo, "--title", title, "--body", text,
               "--label", LABEL, "--assignee", args.assignee)  # fmt: skip
        else:
            gh("issue", "comment", str(number), "--repo", args.repo, "--body", text)
        return 0
    if args.dry_run:
        print(f"would close '{title}' if open")
        return 0
    number = _open_issue(args.repo, title)
    if number is not None:
        gh("issue", "close", str(number), "--repo", args.repo,
           "--comment", f"Passed again: {args.run_url}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
