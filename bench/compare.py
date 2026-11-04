"""A before/after table for BENCHMARKS.md from two bench/run_all.sh results
directories: medians across each directory's runs (ranges in brackets), so a
change's effect is read off numbers both runs measured the same way.

    python bench/compare.py bench/results/<before> bench/results/<after>
"""

import statistics
import sys
from pathlib import Path
from typing import Any

from report import b1_runs, b2_ramps, ceiling, spread


def row(label: str, before: list[float], after: list[float], digits: int = 2) -> str:
    if not before or not after:
        return f"| {label} | not recorded | not recorded | n/a |"
    change = statistics.median(after) - statistics.median(before)
    return (
        f"| {label} | {spread(before, digits)} | {spread(after, digits)} | {change:+.{digits}f} |"
    )


def b1_rows(before: Path, after: Path, kind: str, latency: str, name: str) -> list[str]:
    b, a = b1_runs(before, kind, latency), b1_runs(after, kind, latency)

    def values(runs: list[dict[str, Any]], key: str, p: str) -> list[float]:
        return [r[key][p] for r in runs]

    def throttled(runs: list[dict[str, Any]]) -> list[float]:
        return [float(r["gateway_cpu"]["throttled"]) for r in runs if r["gateway_cpu"]]

    def cpu_ms(runs: list[dict[str, Any]]) -> list[float]:
        return [r["cpu_ms"] for r in runs if r["cpu_ms"] is not None]

    return [
        # The mock, reached directly, is the control: what changed on the client
        # side (k6, the harness, the machine) moves it too.
        *(row(f"{name} mock direct {p} (ms)", values(b, "mock", p), values(a, "mock", p))
          for p in ("p50", "p99")),
        *(row(f"{name} overhead {p} (ms)", values(b, "overhead", p), values(a, "overhead", p))
          for p in ("p50", "p95", "p99")),
        row(f"{name} gateway CPU per request (ms)", cpu_ms(b), cpu_ms(a)),
        row(f"{name} gateway throttled periods per step", throttled(b), throttled(a), 0),
    ]  # fmt: skip


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # cp1252 consoles
    before, after = Path(sys.argv[1]), Path(sys.argv[2])
    lines = [
        f"Before: `{before.as_posix()}`; after: `{after.as_posix()}`. "
        "Medians across each directory's runs (range in brackets).",
        "",
        "| | Before | After | Change (medians) |",
        "|---|---|---|---|",
        *b1_rows(before, after, "plain", "http_req_duration", "B1"),
        *b1_rows(before, after, "stream", "http_req_waiting", "B1b"),
    ]
    for replicas in (1, 3):
        ceilings = [
            [float(ceiling(steps)) for _, steps in b2_ramps(d, replicas)] for d in (before, after)
        ]
        lines.append(row(f"B2 max sustained RPS, {replicas} replica(s)", *ceilings, 0))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
