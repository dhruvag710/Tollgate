"""Tables for BENCHMARKS.md from one bench/run_all.sh results directory.

Reads only k6's raw summaries (<step>.json); every number printed is computed
from them here. Each benchmark ran several times: a table gives the median
across runs and, in brackets, the range (lowest to highest run).

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


def metric(data: dict[str, Any], name: str, scenario: str, stat: str) -> float:
    m = data["metrics"].get(f"{name}{{scenario:{scenario}}}")
    return float(m["values"].get(stat, 0)) if m else 0.0


def side(data: dict[str, Any], scenario: str, latency: str) -> dict[str, float]:
    return {
        "p50": metric(data, latency, scenario, "med"),
        "p95": metric(data, latency, scenario, "p(95)"),
        "p99": metric(data, latency, scenario, "p(99)"),
        "requests": metric(data, "http_reqs", scenario, "count"),
        "error_rate": metric(data, "http_req_failed", scenario, "rate"),
        "dropped": metric(data, "dropped_iterations", scenario, "count"),
    }


def spread(values: list[float], digits: int = 2) -> str:
    if len(values) == 1:
        return f"{values[0]:.{digits}f}"
    low, high = min(values), max(values)
    return f"{statistics.median(values):.{digits}f} ({low:.{digits}f} to {high:.{digits}f})"


def runs(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern), key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)])


def gateway_cpu(step: Path) -> dict[str, float]:
    """The gateways' cgroup CPU counters across a step (summed over replicas):
    CPU seconds used, and 100 ms quota periods in which they were throttled."""

    def read(path: Path) -> dict[str, dict[str, int]]:
        counters = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            name, *fields = line.split()
            counters[name] = {k: int(v) for k, v in zip(fields[::2], fields[1::2], strict=True)}
        return counters

    stem = step.name.removesuffix(".json")
    before = read(step.with_name(f"{stem}.gateway-cpu-before.txt"))
    after = read(step.with_name(f"{stem}.gateway-cpu-after.txt"))

    def delta(key: str) -> int:
        return sum(after[n][key] - before[n][key] for n in after if n in before)

    return {
        "cpu_s": delta("usage_usec") / 1e6,
        "periods": delta("nr_periods"),
        "throttled": delta("nr_throttled"),
        "throttled_s": delta("throttled_usec") / 1e6,
    }


def b1(root: Path, kind: str, latency: str, title: str) -> list[str]:
    files = sorted((root / "b1").glob(f"run-*/{kind}.json"))
    if not files:
        return []
    per_run, cpu = [], []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        mock, gw = side(data, "mock", latency), side(data, "gateway", latency)
        per_run.append((mock, gw, {p: gw[p] - mock[p] for p in ("p50", "p95", "p99")}))
        cpu.append(gateway_cpu(f))
    lines = [
        f"#### {title}",
        "",
        f"`{latency}`, ms: median of {len(per_run)} runs (range across runs).",
        "",
        "| | p50 | p95 | p99 |",
        "|---|---|---|---|",
    ]
    for label, index in (("Mock, direct", 0), ("Through the gateway", 1), ("**Overhead**", 2)):
        cells = [spread([r[index][p] for r in per_run]) for p in ("p50", "p95", "p99")]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    requests = [int(r[1]["requests"]) for r in per_run]
    # The gateway's CPU across the step covers its warm-up and setup requests too.
    served = [
        int(
            metric(
                json.loads(f.read_text(encoding="utf-8")), "http_reqs", "gateway_warmup", "count"
            )
        )
        + n
        for f, n in zip(files, requests, strict=True)
    ]
    errors = [r[1]["error_rate"] for r in per_run]
    dropped = [int(r[1]["dropped"] + r[0]["dropped"]) for r in per_run]
    lines += [
        "",
        f"Requests through the gateway per run: {', '.join(map(str, requests))}; "
        f"failed: {', '.join(f'{100 * e:.2f}%' for e in errors)}; "
        f"requests k6 couldn't start on time (both sides): {', '.join(map(str, dropped))}.",
        "",
        "Gateway CPU per request, from its cgroup counters across the step: "
        + ", ".join(f"{1000 * c['cpu_s'] / n:.2f} ms" for c, n in zip(cpu, served, strict=True))
        + "; 100 ms CPU-quota periods in which it was throttled: "
        + ", ".join(f"{c['throttled']} of {c['periods']}" for c in cpu)
        + ".",
        "",
    ]
    return lines


def b2_step(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
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
    rate = int(re.findall(r"\d+", path.name)[0])
    served = gw["requests"] + metric(data, "http_reqs", "gateway_warmup", "count")
    cpu_ms = 1000 * gateway_cpu(path)["cpu_s"] / served if served else 0.0
    return {
        "rate": rate,
        "overhead": overhead,
        "gw": gw,
        "mock": mock,
        "reasons": reasons,
        "cpu_ms": cpu_ms,
    }


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
        for run_dir in runs(root / "b2", "run-*"):
            steps = [b2_step(p) for p in runs(run_dir / f"replicas-{replicas}", "rate-*[0-9].json")]
            if not steps:
                continue
            held = [s for s in steps if not s["reasons"]]
            failed = next((s for s in steps if s["reasons"]), None)
            ceilings.append(held[-1]["rate"] if held else 0)
            endings.append(
                f"{failed['rate']} RPS: {', '.join(failed['reasons'])}" if failed else "step cap"
            )
            detail += [
                f"{run_dir.name}, {replicas} replica(s):",
                "",
                "| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) "
                "| Failed | Not started | Gateway CPU per request (ms) | Held |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for s in steps:
                detail.append(
                    f"| {s['rate']} | {s['gw']['p50']:.1f} / {s['gw']['p99']:.1f} "
                    f"| {s['mock']['p99']:.1f} | {s['overhead']:.1f} "
                    f"| {100 * s['gw']['error_rate']:.2f}% | {int(s['gw']['dropped'])} "
                    f"| {s['cpu_ms']:.2f} | {'yes' if not s['reasons'] else 'no'} |"
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


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # cp1252 consoles
    root = Path(sys.argv[1])
    env = (root / "environment.txt").read_text(encoding="utf-8").splitlines()
    out = [f"### Results: `{root.as_posix()}`", "", "```", *env[:8], "```", ""]
    out += b1(root, "plain", "http_req_duration", "B1: gateway overhead, non-streamed")
    out += b1(root, "stream", "http_req_waiting", "B1b: time to first chunk, streamed")
    summary, detail = b2(root)
    out += summary + detail
    print("\n".join(out))


if __name__ == "__main__":
    main()
