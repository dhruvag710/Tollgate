"""Tables for BENCHMARKS.md from one bench/run_all.sh results directory.

Reads only what the harness recorded: k6's raw summaries (<step>.json) and
the containers' cgroup CPU counters around each step. Every number printed is
computed from them here. Each benchmark ran several times: a table gives the
median across runs and, in brackets, the range (lowest to highest run).

    python bench/report.py bench/results/<timestamp>
"""

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

MAX_P99_OVERHEAD_MS = 50  # B2's stopping rule, as in bench/k6/chat.js
MAX_ERROR_RATE = 0.001
STATUS_TAG = re.compile(r"^http_reqs\{scenario:(\w+),status:(\d+)\}$")


def metric(data: dict[str, Any], name: str, scenario: str, stat: str) -> float:
    m = data["metrics"].get(f"{name}{{scenario:{scenario}}}")
    return float(m["values"].get(stat, 0)) if m else 0.0


def statuses(data: dict[str, Any], scenario: str) -> dict[str, int] | None:
    """{status: requests} for a measured phase; None if the run didn't record them."""
    counts: dict[str, int] = {}
    found = False
    for key, m in data["metrics"].items():
        match = STATUS_TAG.match(key)
        if match and match.group(1) == scenario:
            found = True
            if m["values"]["count"]:
                counts[match.group(2)] = int(m["values"]["count"])
    if not found:
        return None
    other = int(metric(data, "http_reqs", scenario, "count")) - sum(counts.values())
    if other:
        counts["other"] = other
    return counts


def side(data: dict[str, Any], scenario: str, latency: str) -> dict[str, Any]:
    return {
        "p50": metric(data, latency, scenario, "med"),
        "p95": metric(data, latency, scenario, "p(95)"),
        "p99": metric(data, latency, scenario, "p(99)"),
        "requests": metric(data, "http_reqs", scenario, "count"),
        "error_rate": metric(data, "http_req_failed", scenario, "rate"),
        "dropped": metric(data, "dropped_iterations", scenario, "count"),
        "statuses": statuses(data, scenario),
        "incomplete": int(metric(data, "incomplete_streams", scenario, "count")),
    }


def spread(values: list[float], digits: int = 2) -> str:
    if len(values) == 1:
        return f"{values[0]:.{digits}f}"
    low, high = min(values), max(values)
    return f"{statistics.median(values):.{digits}f} ({low:.{digits}f} to {high:.{digits}f})"


def runs(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern), key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)])


def cpu(step: Path, service: str) -> dict[str, float] | None:
    """A service's cgroup CPU counters across a step, summed over its containers:
    CPU seconds, CFS periods, periods in which it was throttled and for how long.
    None if they weren't recorded for that service."""

    def read(path: Path) -> dict[str, dict[str, int]]:
        counters = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            name, *fields = line.split()
            counters[name] = {k: int(v) for k, v in zip(fields[::2], fields[1::2], strict=True)}
        return counters

    stem = step.name.removesuffix(".json")
    for before_name, after_name in (
        (f"{stem}.cpu-before.txt", f"{stem}.cpu-after.txt"),
        (f"{stem}.gateway-cpu-before.txt", f"{stem}.gateway-cpu-after.txt"),  # earlier runs
    ):
        if step.with_name(before_name).exists():
            before = read(step.with_name(before_name))
            after = read(step.with_name(after_name))
            break
    else:
        return None
    names = [n for n in after if n in before and f"-{service}-" in n]
    if not names:
        return None

    def delta(key: str) -> int:
        return sum(after[n][key] - before[n][key] for n in names)

    return {
        "cpu_s": delta("usage_usec") / 1e6,
        "periods": delta("nr_periods"),
        "throttled": delta("nr_throttled"),
        "throttled_s": delta("throttled_usec") / 1e6,
    }


def throttling(c: dict[str, float] | None) -> str:
    if c is None:
        return "not recorded"
    if not c["throttled"]:
        return f"0 of {c['periods']}"
    average_ms = 1000 * c["throttled_s"] / c["throttled"]
    return f"{c['throttled']} of {c['periods']} (avg {average_ms:.0f} ms each)"


def non_200(s: dict[str, Any]) -> str:
    if s["statuses"] is None:
        return "not recorded"
    bad = {k: v for k, v in s["statuses"].items() if k != "200"}
    parts = [f"{n} with status {code}" for code, n in sorted(bad.items())]
    if s["incomplete"]:
        parts.append(f"{s['incomplete']} streams without [DONE]")
    return ", ".join(parts) or "none"


def load(step: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(step.read_text(encoding="utf-8"))
    return data


def b1_runs(root: Path, kind: str, latency: str) -> list[dict[str, Any]]:
    """Per run: both sides, the overhead, and the services' CPU counters."""
    out = []
    for f in sorted((root / "b1").glob(f"run-*/{kind}.json")):
        data = load(f)
        mock, gw = side(data, "mock", latency), side(data, "gateway", latency)
        served = gw["requests"] + metric(data, "http_reqs", "gateway_warmup", "count")
        gateway_cpu = cpu(f, "gateway")
        out.append(
            {
                "mock": mock,
                "gateway": gw,
                "overhead": {p: gw[p] - mock[p] for p in ("p50", "p95", "p99")},
                "gateway_cpu": gateway_cpu,
                "mock_cpu": cpu(f, "mock_provider"),
                # Across the step, the gateway's CPU covers its warm-up and setup too.
                "cpu_ms": 1000 * gateway_cpu["cpu_s"] / served if gateway_cpu else None,
            }
        )
    return out


def b1(root: Path, kind: str, latency: str, title: str) -> list[str]:
    per_run = b1_runs(root, kind, latency)
    if not per_run:
        return []
    lines = [
        f"#### {title}",
        "",
        f"`{latency}`, ms: median of {len(per_run)} runs (range across runs).",
        "",
        "| | p50 | p95 | p99 |",
        "|---|---|---|---|",
    ]
    for label, key in (("Mock, direct", "mock"), ("Through the gateway", "gateway")):
        cells = [spread([r[key][p] for r in per_run]) for p in ("p50", "p95", "p99")]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    cells = [spread([r["overhead"][p] for r in per_run]) for p in ("p50", "p95", "p99")]
    lines.append("| **Overhead** | " + " | ".join(cells) + " |")
    lines += [
        "",
        "| Run | Requests (gateway) | Failed | Not started on time | Responses other than "
        "a complete 200 | Gateway CPU per request | Gateway throttled periods "
        "| Mock throttled periods |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(per_run, 1):
        gw, mock = r["gateway"], r["mock"]
        cpu_ms = f"{r['cpu_ms']:.2f} ms" if r["cpu_ms"] is not None else "not recorded"
        lines.append(
            f"| {i} | {int(gw['requests'])} | {100 * gw['error_rate']:.2f}% "
            f"| {int(gw['dropped'] + mock['dropped'])} | {non_200(gw)} | {cpu_ms} "
            f"| {throttling(r['gateway_cpu'])} | {throttling(r['mock_cpu'])} |"
        )
    return [*lines, ""]


def b2_step(path: Path) -> dict[str, Any]:
    data = load(path)
    mock = side(data, "mock", "http_req_duration")
    gw = side(data, "gateway", "http_req_duration")
    overhead = gw["p99"] - mock["p99"]
    mock_held = mock["error_rate"] <= MAX_ERROR_RATE and mock["dropped"] == 0
    reasons = []
    if not mock_held:
        reasons.append("the mock itself fell behind")
    if overhead > MAX_P99_OVERHEAD_MS:
        reasons.append(f"p99 overhead {overhead:.0f} ms")
    if gw["error_rate"] > MAX_ERROR_RATE:
        reasons.append(f"{100 * gw['error_rate']:.2f}% failed")
    if gw["dropped"]:
        reasons.append(f"{int(gw['dropped'])} requests not started on time")
    served = gw["requests"] + metric(data, "http_reqs", "gateway_warmup", "count")
    gateway_cpu = cpu(path, "gateway")
    return {
        "rate": int(re.findall(r"\d+", path.name)[0]),
        "overhead": overhead,
        "gw": gw,
        "mock": mock,
        "reasons": reasons,
        "gateway_cpu": gateway_cpu,
        "cpu_ms": 1000 * gateway_cpu["cpu_s"] / served if gateway_cpu and served else 0.0,
    }


def b2_ramps(root: Path, replicas: int) -> list[tuple[str, list[dict[str, Any]]]]:
    ramps = []
    for run_dir in runs(root / "b2", "run-*"):
        steps = [b2_step(p) for p in runs(run_dir / f"replicas-{replicas}", "rate-*[0-9].json")]
        if steps:
            ramps.append((run_dir.name, steps))
    return ramps


def ceiling(steps: list[dict[str, Any]]) -> int:
    held = [s["rate"] for s in steps if not s["reasons"]]
    return held[-1] if held else 0


def b2(root: Path) -> tuple[list[str], list[str]]:
    summary = [
        "#### B2: throughput ceiling (through nginx)",
        "",
        "Highest RPS step that held (p99 overhead ≤ 50 ms, ≤ 0.1% failed, every request "
        "started on time): median of runs (range across runs).",
        "",
        "| Gateway replicas | Max sustained RPS | Per run | What ended each ramp |",
        "|---|---|---|---|",
    ]
    detail = ["#### B2 steps, every run", ""]
    medians: dict[int, float] = {}
    for replicas in (1, 3):
        ceilings, endings = [], []
        for run_name, steps in b2_ramps(root, replicas):
            failed = next((s for s in steps if s["reasons"]), None)
            ceilings.append(ceiling(steps))
            endings.append(
                f"{failed['rate']} RPS: {', '.join(failed['reasons'])}" if failed else "step cap"
            )
            detail += [
                f"{run_name}, {replicas} replica(s):",
                "",
                "| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed "
                "| Not started | Other than 200 | Gateway CPU per request (ms) "
                "| Gateway throttled periods | Held |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for s in steps:
                detail.append(
                    f"| {s['rate']} | {s['gw']['p50']:.1f} / {s['gw']['p99']:.1f} "
                    f"| {s['mock']['p99']:.1f} | {s['overhead']:.1f} "
                    f"| {100 * s['gw']['error_rate']:.2f}% | {int(s['gw']['dropped'])} "
                    f"| {non_200(s['gw'])} | {s['cpu_ms']:.2f} | {throttling(s['gateway_cpu'])} "
                    f"| {'yes' if not s['reasons'] else 'no'} |"
                )
            detail.append("")
        if ceilings:
            medians[replicas] = statistics.median(ceilings)
            summary.append(
                f"| {replicas} | {spread([float(c) for c in ceilings], 0)} "
                f"| {', '.join(map(str, ceilings))} | {'; '.join(endings)} |"
            )
    if 1 in medians and 3 in medians and medians[1]:
        ratio = medians[3] / medians[1]
        summary += ["", f"3 replicas sustain {ratio:.2f} times what one does (medians)."]
    return [*summary, ""], detail


def environment(root: Path) -> list[str]:
    lines = (root / "environment.txt").read_text(encoding="utf-8").splitlines()
    end = next((i for i, line in enumerate(lines) if line.startswith("--- ")), len(lines))
    return lines[:end]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # cp1252 consoles
    root = Path(sys.argv[1])
    out = [f"### Results: `{root.as_posix()}`", "", "```", *environment(root), "```", ""]
    out += b1(root, "plain", "http_req_duration", "B1: gateway overhead, non-streamed")
    out += b1(root, "stream", "http_req_waiting", "B1b: time to first chunk, streamed")
    summary, detail = b2(root)
    out += summary + detail
    print("\n".join(out))


if __name__ == "__main__":
    main()
