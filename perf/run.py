"""Runs a benchmark plan on the load VM: ramps, failovers, and the results to publish.

    python perf/run.py plan.json --out results/<run>

A plan names its targets (where to send the load and how) and what to run on them:

- a **ramp** raises the load step by step (``steps``, requests a minute) until a step fails,
  then tries the middle between the last step that passed and the one that failed. A step
  passes when at most ``max_error_rate`` of its requests failed, it delivered at least
  ``min_achieved`` of the load it was offered, and its p95 latency stayed within
  ``max_p95_ratio`` of the baseline's (the same call shape sent straight to the mock). A step
  where the load VM itself ran above ``max_client_cpu`` doesn't count: the ramp stops there,
  marked ``client_limited``.
- a **failover** holds one load while the control app makes mocks fail on a schedule
  (``[[second, region, status], ...]``) and records every second.

Every step writes its summary (``<name>.json``) and every request (``<name>.csv.gz``), and
``index.json`` lists them with their times, for ``collect.py`` to fetch the gateway's metrics.
Keys come from a file perf/ctl.py put on the VM (``key_source: "file:<name>"``), the VM's managed
identity (``"imds:<resource>"``) or an environment variable (``key_env``), and are never written
to the results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loadgen import Config, run, summarize, write_samples  # noqa: E402

CRITERIA = {
    "max_error_rate": 0.01,
    "min_achieved": 0.95,
    "max_p95_ratio": 1.25,
    "max_client_cpu": 85.0,
}


def _imds(resource: str) -> str:
    url = (
        "http://169.254.169.254/metadata/identity/oauth2/token"
        f"?api-version=2018-02-01&resource={resource}"
    )
    request = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return str(json.load(response)["access_token"])


def _key(target: dict[str, Any]) -> str:
    """A target's key: ``file:<name>`` (``.keys/<name>``, which perf/ctl.py writes),
    ``imds:<resource>`` (a token of the VM's managed identity for that audience, e.g. Foundry),
    or the environment variable ``key_env``."""
    source = target.get("key_source", "")
    if source.startswith("file:"):
        return str(
            (Path(__file__).resolve().parent / ".keys" / source.removeprefix("file:"))
            .read_text()
            .strip()
        )
    if source.startswith("imds:"):
        return _imds(source.removeprefix("imds:"))
    return os.environ.get(target.get("key_env", ""), "")


def _control(control_url: str, region: str, status: str) -> None:
    data = json.dumps({"status": status}).encode()
    request = urllib.request.Request(f"{control_url}/state/{region}", data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10):
        pass


def _reset(control_url: str) -> None:
    if control_url:
        request = urllib.request.Request(f"{control_url}/reset", data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=10):
            pass


class Runner:
    def __init__(self, plan: dict[str, Any], out: Path) -> None:
        self.plan = plan
        self.out = out
        self.criteria = {**CRITERIA, **plan.get("criteria", {})}
        self.control_url = plan.get("control_url", "").rstrip("/")
        self.workers = int(plan.get("workers", max(1, (os.cpu_count() or 2) - 2)))
        index_path = out / "index.json"
        self.index: list[dict[str, Any]] = (
            json.loads(index_path.read_text()) if index_path.exists() else []
        )
        self.baselines: dict[str, float] = {}
        out.mkdir(parents=True, exist_ok=True)

    def _save_index(self) -> None:
        (self.out / "index.json").write_text(json.dumps(self.index, indent=2))

    def _done(self, name: str) -> dict[str, Any] | None:
        """A step that an earlier, interrupted run of the plan already finished."""
        path = self.out / f"{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def step(
        self,
        name: str,
        target_name: str,
        shape: str,
        rpm: float,
        warmup: float,
        duration: float,
        schedule: list[list[Any]] | None = None,
    ) -> dict[str, Any]:
        done = self._done(name)
        if done is not None:
            print(f"{name}: already done", flush=True)
            return done
        target = self.plan["targets"][target_name]
        config = Config(
            url=target["url"],
            model=target["model"],
            shape=shape,
            rpm=rpm,
            duration=duration,
            warmup=warmup,
            workers=min(self.workers, max(1, int(rpm // 30) or 1)),
            key_header=target.get("key_header", "Authorization"),
            member_header=target.get("member_header", "x-litellm-model-id"),
            timeout=float(target.get("timeout", 300)),
            query=target.get("query", ""),
            extra=target.get("extra", {}),
        )
        _reset(self.control_url)
        follow: threading.Thread | None = None
        if schedule:
            started = time.monotonic() + 2.0

            def play() -> None:
                for second, region, status in schedule:
                    time.sleep(max(0.0, started + float(second) - time.monotonic()))
                    _control(self.control_url, region, status)
                    print(f"  t={second}s {region} -> {status}", flush=True)

            follow = threading.Thread(target=play, daemon=True)
            follow.start()
        print(f"{name}: {target_name} {shape} {rpm:g} rpm, {warmup:g}+{duration:g} s", flush=True)
        result = run(config, _key(target))
        if follow:
            follow.join(timeout=5)
        _reset(self.control_url)
        summary = summarize(result)
        summary.update(name=name, target=target_name, ended_at=time.time(), schedule=schedule)
        (self.out / f"{name}.json").write_text(json.dumps(summary, indent=2))
        write_samples(result, self.out / f"{name}.csv.gz")
        self.index.append(
            {
                "name": name,
                "target": target_name,
                "shape": shape,
                "rpm": rpm,
                "started_at": summary["started_at"],
                "ended_at": summary["ended_at"],
            }
        )
        self._save_index()
        lat = summary["latency_ms"]
        print(
            f"  ok {summary['ok']}/{summary['started']}  achieved {summary['achieved_rpm']:g} rpm  "
            f"errors {summary['errors']}  p50 {lat['p50']}  p95 {lat['p95']}  "
            f"client cpu {summary['client_cpu']}",
            flush=True,
        )
        return summary

    def verdict(self, summary: dict[str, Any], baseline_p95: float | None) -> str:
        c = self.criteria
        cpu = summary["client_cpu"]["avg"]
        if cpu is not None and cpu > c["max_client_cpu"]:
            return "client_limited"
        if summary["error_rate"] is None or summary["error_rate"] > c["max_error_rate"]:
            return "errors"
        if summary["achieved_rpm"] < c["min_achieved"] * summary["offered_rpm"]:
            return "throughput"
        p95 = summary["latency_ms"]["p95"]
        if baseline_p95 and p95 and p95 > c["max_p95_ratio"] * baseline_p95:
            return "latency"
        return "pass"

    def baseline(self, shape: str) -> float | None:
        """The p95 latency of the call shape sent straight to the mock, at a light load."""
        if shape in self.baselines:
            return self.baselines[shape]
        base = self.plan.get("baseline")
        if not base:
            return None
        summary = self.step(
            f"baseline-{shape}",
            base["target"],
            shape,
            base.get("rpm", 60),
            base.get("warmup", 30),
            base.get("duration", 120),
        )
        self.baselines[shape] = summary["latency_ms"]["p95"]
        return self.baselines[shape]

    def ramp(self, spec: dict[str, Any]) -> dict[str, Any]:
        target, shape = spec["target"], spec["shape"]
        baseline_p95 = self.baseline(shape) if spec.get("compare", True) else None
        warmup, duration = spec.get("warmup", 60), spec.get("duration", 180)
        label = spec.get("label", target)
        passed: float | None = None
        failed: tuple[float, str] | None = None
        steps: list[dict[str, Any]] = []

        def one(rpm: float) -> str:
            summary = self.step(f"{label}-{shape}-{int(rpm)}", target, shape, rpm, warmup, duration)
            verdict = self.verdict(summary, baseline_p95)
            steps.append({"rpm": rpm, "verdict": verdict, "name": summary["name"]})
            print(f"  -> {verdict}", flush=True)
            return verdict

        # all_steps: run every step even after one fails (a real model's quota spilling over).
        for rpm in spec["steps"]:
            verdict = one(rpm)
            if verdict == "pass":
                if not failed:
                    passed = rpm
                continue
            failed = failed or (rpm, verdict)
            if not spec.get("all_steps"):
                break
        bisect = int(spec.get("bisect", 1))
        while failed and failed[1] != "client_limited" and bisect > 0:
            low = passed or 0
            middle = round((low + failed[0]) / 2 / 60) * 60
            if middle <= low or middle >= failed[0]:
                break
            verdict = one(middle)
            if verdict == "pass":
                passed = middle
            else:
                failed = (middle, verdict)
            bisect -= 1
        outcome = {
            "label": label,
            "target": target,
            "shape": shape,
            "max_passing_rpm": passed,
            "first_failure": {"rpm": failed[0], "reason": failed[1]} if failed else None,
            "baseline_p95_ms": baseline_p95,
            "steps": steps,
        }
        (self.out / f"ramp-{label}-{shape}.json").write_text(json.dumps(outcome, indent=2))
        return outcome

    def failover(self, spec: dict[str, Any]) -> dict[str, Any]:
        label = spec.get("label", spec["target"])
        return self.step(
            f"failover-{label}-{spec['shape']}-{int(spec['rpm'])}",
            spec["target"],
            spec["shape"],
            spec["rpm"],
            0,
            spec.get("duration", 600),
            schedule=spec["schedule"],
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", help="run only the ramps/failovers whose label has this text")
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text())
    runner = Runner(plan, args.out)
    for spec in plan.get("ramps", []):
        if args.only and args.only not in spec.get("label", spec["target"]):
            continue
        outcome = runner.ramp(spec)
        print(
            f"== {outcome['label']} {outcome['shape']}: max {outcome['max_passing_rpm']} rpm, "
            f"then {outcome['first_failure']}",
            flush=True,
        )
    for spec in plan.get("failovers", []):
        if args.only and args.only not in spec.get("label", spec["target"]):
            continue
        runner.failover(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
