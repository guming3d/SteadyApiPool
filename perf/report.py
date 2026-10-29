"""Turns benchmark results (perf/results/<run>/) into the tables the docs publish.

    python perf/report.py results/apim-1 results/aca-1 ... --out perf/report.json

For every ramp: the highest load that passed and why the next step failed, and at each step
the latency added over the baseline (the same call straight to the mock). For every failover:
per phase of the schedule, the clients' errors, their p95 latency and which members answered,
and how long the gateway took to move traffic off the failing member and back. For the real
model: each step's answers, 429s and latency, straight to one deployment and through each pool.
It prints Markdown and writes everything to ``--out`` for the docs.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


def _pct(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def _samples(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ramps(folder: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(folder.glob("ramp-*.json")):
        ramp = json.loads(path.read_text())
        steps = []
        for step in ramp["steps"]:
            summary = json.loads((folder / f"{step['name']}.json").read_text())
            steps.append(
                {
                    "rpm": step["rpm"],
                    "verdict": step["verdict"],
                    "ok": summary["ok"],
                    "started": summary["started"],
                    "errors": summary["errors"],
                    "latency_ms": summary["latency_ms"],
                    "ttft_ms": summary["ttft_ms"],
                    "members": summary["members"],
                    "prompt_tokens": summary["prompt_tokens"],
                    "completion_tokens": summary["completion_tokens"],
                    "client_cpu": summary["client_cpu"],
                }
            )
        baseline = None
        base_path = folder / f"baseline-{ramp['shape']}.json"
        if base_path.exists():
            base = json.loads(base_path.read_text())
            baseline = {"latency_ms": base["latency_ms"], "ttft_ms": base["ttft_ms"]}
        out.append({**ramp, "steps": sorted(steps, key=lambda s: s["rpm"]), "baseline": baseline})
    return out


def failovers(folder: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(folder.glob("failover-*.json")):
        summary = json.loads(path.read_text())
        rows = _samples(path.with_suffix("").with_suffix(".csv.gz"))
        schedule = summary["schedule"]
        edges = [0.0] + [float(second) for second, _, _ in schedule] + [math.inf]
        labels = ["healthy"] + [f"{region} {status}" for _, region, status in schedule]
        phases = []
        for i, label in enumerate(labels):
            low, high = edges[i], edges[i + 1]
            window = [r for r in rows if low <= float(r["start"]) < high]
            if not window:
                continue
            ok = [r for r in window if r["status"] == "200" and not r["error"]]
            kinds = Counter(
                r["error"] or ("ok" if r["status"] == "200" else r["status"]) for r in window
            )
            members = Counter(r["member"] or "?" for r in ok)
            latencies = [float(r["latency_ms"]) for r in ok]
            # Seconds into the phase until 95% of each second's requests come from other members
            # (a fault) or from mock-a again (healthy after one): how fast the gateway moved.
            moved = None
            if i > 0:
                target_other = labels[i].split()[-1] != "200"
                per_second: dict[int, Counter[str]] = {}
                for r in ok:
                    second = int(float(r["start"]) - low)
                    per_second.setdefault(second, Counter())[r["member"] or "?"] += 1
                for second in sorted(per_second):
                    counts = per_second[second]
                    total = sum(counts.values())
                    share_a = counts.get("mock-a", 0) / total if total else 0
                    if (target_other and share_a <= 0.05) or (not target_other and share_a >= 0.95):
                        moved = second
                        break
            phases.append(
                {
                    "phase": label,
                    "from_s": low,
                    "to_s": min(high, max(float(r["start"]) for r in window) + 1),
                    "requests": len(window),
                    "errors": {k: v for k, v in kinds.items() if k != "ok"},
                    "error_rate": round(1 - len(ok) / len(window), 4),
                    "p50_ms": _pct(latencies, 50),
                    "p95_ms": _pct(latencies, 95),
                    "members": dict(members.most_common()),
                    "moved_after_s": moved,
                }
            )
        out.append(
            {
                "name": summary["name"],
                "target": summary["target"],
                "rpm": summary["offered_rpm"],
                "phases": phases,
            }
        )
    return out


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def markdown(data: dict[str, Any]) -> str:
    lines = []
    for run, content in data.items():
        lines.append(f"## {run}\n")
        for ramp in content["ramps"]:
            base = ramp["baseline"]
            top = ramp["steps"][-1]["rpm"] if ramp["steps"] else None
            ceiling = ramp["max_passing_rpm"]
            mark = "≥ " if ceiling and ceiling == max(s["rpm"] for s in ramp["steps"]) else ""
            lines.append(
                f"### {ramp['label']} · {ramp['shape']}: max {mark}{ceiling} RPM"
                f" (then {ramp['first_failure']})  top step {top}\n"
            )
            lines.append(
                "| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 "
                "| members |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for s in ramp["steps"]:
                lat = s["latency_ms"]
                plus50 = lat["p50"] - base["latency_ms"]["p50"] if base and lat["p50"] else None
                plus95 = lat["p95"] - base["latency_ms"]["p95"] if base and lat["p95"] else None
                lines.append(
                    f"| {s['rpm']:,g} | {s['verdict']} | {s['ok']}/{s['started']} "
                    f"| {s['errors'] or ''} | "
                    f"{_ms(lat['p50'])} | {_ms(lat['p95'])} | {_ms(lat['p99'])} | {_ms(plus50)} | "
                    f"{_ms(plus95)} | {_ms(s['ttft_ms']['p95'])} | {s['members']} |"
                )
            lines.append("")
        for failover in content["failovers"]:
            lines.append(f"### failover {failover['target']} at {failover['rpm']:g} RPM\n")
            lines.append(
                "| phase | requests | error rate | errors | p50 | p95 | moved after | members |"
            )
            lines.append("|---|---|---|---|---|---|---|---|")
            for p in failover["phases"]:
                moved = "-" if p["moved_after_s"] is None else f"{p['moved_after_s']} s"
                lines.append(
                    f"| {p['phase']} | {p['requests']} | {p['error_rate']:.2%} "
                    f"| {p['errors'] or ''} | {_ms(p['p50_ms'])} | {_ms(p['p95_ms'])} | {moved} "
                    f"| {p['members']} |"
                )
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("folders", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    data = {
        folder.name: {"ramps": ramps(folder), "failovers": failovers(folder)}
        for folder in args.folders
    }
    if args.out:
        args.out.write_text(json.dumps(data, indent=2))
    print(markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
