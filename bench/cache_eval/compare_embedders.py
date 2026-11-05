"""B4's embedding model comparison (SPEC §9): the four small models, on recall
at the chosen threshold (the lowest with at most 2% false hits on both sets),
CPU embedding latency per query, and memory.

    python bench/cache_eval/compare_embedders.py

Each model runs in a process of its own, so one model's memory isn't counted
against the next. In it: the threshold sweep (as eval_threshold.py, using
every core, which changes nothing but the time it takes), then latency the way
the gateway embeds: one query at a time, on one PyTorch thread
(SEMANTIC_CACHE_THREADS), after a warm-up. Models load from the local cache
(HF_HUB_OFFLINE), so no download is timed: fetch them first.

Writes bench/results/b4-<UTC stamp>/: compare.md and compare.json (the models
side by side, then false hits and recall by category for each, every rate with
its counts), and each model's sweep, table and charts in a directory of its
own. The winner by the rule below becomes SEMANTIC_CACHE_MODEL; its chosen
threshold, SEMANTIC_CACHE_THRESHOLD. Run on a quiet machine: the latency
numbers are the point. Each timed run is watched (machine.Watch); a flagged
one is rerun into the same directory with `--models <model> --into <dir>`,
which measures that model again and rebuilds compare.md. Needs `.[bench]`.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eval_threshold import results_dir, slug, write
from machine import Watch, as_text, describe
from pairs import load_adversarial, load_qqp
from scoring import (
    MAX_FALSE_HIT_RATE,
    Rates,
    Report,
    evaluate,
    false_hits_text,
    percentile,
    recall_text,
)

MODELS = (
    "BAAI/bge-small-en-v1.5",
    "sentence-transformers/all-MiniLM-L6-v2",
    "intfloat/e5-small-v2",
    "thenlper/gte-small",
)
WARM_UP = 20
LATENCY_QUERIES = 500
# The winner: the highest QQP recall at its chosen threshold; within this
# much of the best, the lowest p99 latency.
RECALL_TIE = 0.01


def measure(model: str, out: Path) -> dict[str, Any]:
    """One model, in this process: quality, latency, memory."""
    import psutil
    import sentence_transformers  # noqa: F401  # imported before the baseline: not the model's cost
    import torch

    from tollgate.cache.embedder import SentenceTransformerEmbedder

    process = psutil.Process()
    cores = psutil.cpu_count(logical=False) or 1
    before = process.memory_info().rss
    started = time.perf_counter()
    embedder = SentenceTransformerEmbedder(model, threads=cores)
    load_s = time.perf_counter() - started
    memory = process.memory_info().rss - before

    qqp, adversarial = load_qqp(), load_adversarial()
    report = evaluate(embedder.embed_many, qqp, adversarial)

    torch.set_num_threads(1)  # as the gateway runs it
    queries = [p.a for p in qqp][: WARM_UP + LATENCY_QUERIES]
    for text in queries[:WARM_UP]:
        embedder.embed(text)
    timings = []
    with Watch() as watch:
        for i, text in enumerate(queries[WARM_UP:]):
            t = time.perf_counter()
            embedder.embed(text)
            timings.append((time.perf_counter() - t) * 1000)
            if i % 25 == 0:
                watch.sample()

    result = summarize(
        model, report, dimensions=embedder.dimensions, load_s=load_s, memory_bytes=memory,
        timings=timings, torch_threads=torch.get_num_threads(),
    )  # fmt: skip
    result["latency_ms"]["machine"] = watch.result()
    write(model, report, out, describe(1), {"comparison": result})
    return result


def summarize(
    model: str,
    report: Report,
    *,
    dimensions: int,
    load_s: float,
    memory_bytes: int,
    timings: list[float],
    torch_threads: int,
) -> dict[str, Any]:
    """One model's line in the comparison: its rates at its chosen threshold
    (the sweep's highest if none was chosen), each with its counts, overall
    and by category; latency; memory."""
    row = report.reported
    return {
        "model": model,
        "dimensions": dimensions,
        "load_s": load_s,
        "memory_bytes": memory_bytes,
        "latency_ms": {
            "p50": percentile(timings, 50),
            "p99": percentile(timings, 99),
            "queries": len(timings),
            "torch_threads": torch_threads,
        },
        "chosen_threshold": None if report.chosen is None else report.chosen.threshold,
        "reported_at": row.threshold,
        "qqp": row.qqp.as_json(),
        "adversarial": row.adversarial.as_json(),
        "categories": {c: r.as_json() for c, r in row.categories.items()},
    }


def winner(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [r for r in results if r["chosen_threshold"] is not None]
    if not eligible:
        return None
    best = max(r["qqp"]["recall"] for r in eligible)
    close = [r for r in eligible if r["qqp"]["recall"] >= best - RECALL_TIE]
    return min(close, key=lambda r: r["latency_ms"]["p99"])


def table(results: list[dict[str, Any]], chosen: dict[str, Any] | None) -> str:
    lines = [
        "| Model | Threshold chosen | QQP recall | Hard-positive recall | QQP false hits | "
        "Near-miss false hits | Embed p50 / p99 (ms) | Memory (MB) | Load (s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        name = f"**{r['model']}**" if r is chosen else r["model"]
        if r["chosen_threshold"] is None:
            threshold, cells = f"none meets {MAX_FALSE_HIT_RATE:.0%}", ["n/a"] * 4
        else:
            qqp, adv = Rates.from_json(r["qqp"]), Rates.from_json(r["adversarial"])
            threshold = f"{r['chosen_threshold']:.2f}"
            cells = [recall_text(qqp), recall_text(adv), false_hits_text(qqp), false_hits_text(adv)]
        lines.append(
            f"| {name} | {threshold} | {' | '.join(cells)} | "
            f"{r['latency_ms']['p50']:.1f} / {r['latency_ms']['p99']:.1f} | "
            f"{r['memory_bytes'] / 2**20:.0f} | {r['load_s']:.1f} |"
        )
    return "\n".join(lines)


def category_table(results: list[dict[str, Any]]) -> str:
    """False hits and recall by category, a column per model, each at its
    chosen threshold (the sweep's highest where none was chosen)."""

    def heading(r: dict[str, Any]) -> str:
        name = r["model"].split("/")[-1]
        if r["chosen_threshold"] is None:
            return f"{name} (none chosen; at {r['reported_at']:.2f})"
        return f"{name} at {r['chosen_threshold']:.2f}"

    lines = [
        "| Pairs | " + " | ".join(heading(r) for r in results) + " |",
        "|---|" + "---|" * len(results),
    ]

    def add(label: str, show: Callable[[Rates], str], each: list[Rates]) -> None:
        lines.append(f"| {label} | " + " | ".join(show(x) for x in each) + " |")

    def overall(key: str) -> list[Rates]:
        return [Rates.from_json(r[key]) for r in results]

    def within(category: str) -> list[Rates]:
        return [Rates.from_json(r["categories"][category]) for r in results]

    categories = list(results[0]["categories"].items()) if results else []
    add("False hits: QQP non-duplicates", false_hits_text, overall("qqp"))
    for category, first in categories:
        if first["negatives"]:
            add(f"False hits: {category}", false_hits_text, within(category))
    add("False hits: all near-misses", false_hits_text, overall("adversarial"))
    add("Recall: QQP duplicates", recall_text, overall("qqp"))
    for category, first in categories:
        if first["positives"]:
            add(f"Recall: {category}", recall_text, within(category))
    return "\n".join(lines)


def comparison(results: list[dict[str, Any]], chosen: dict[str, Any] | None) -> str:
    """compare.md."""
    duplicates = results[0]["qqp"]["positives"] if results else 0
    verdict = (
        f"No model keeps false hits at or below {MAX_FALSE_HIT_RATE:.0%} on both sets at any "
        "threshold from 0.80 to 0.99."
        if chosen is None
        else f"Winner: `{chosen['model']}` at threshold {chosen['chosen_threshold']:.2f}: the "
        "highest QQP recall at its chosen threshold, or, among the models within "
        f"{RECALL_TIE * 100:.0f} percentage point of it ({RECALL_TIE * duplicates:.0f} of "
        f"{duplicates:,} QQP duplicates), the lowest p99 latency."
    )
    lines = [
        "### B4: embedding models compared",
        "",
        f"Embed: {LATENCY_QUERIES} single queries per model, one PyTorch thread, after "
        f"{WARM_UP} to warm up; the miss path's share before the Postgres lookup (B5 times "
        "that). Memory: resident memory added by loading the model. Rates are at each "
        "model's chosen threshold, with the counts they come from.",
        "",
        table(results, chosen),
        "",
        verdict,
        "",
        "False hits and recall by category, at each model's chosen threshold:",
        "",
        category_table(results),
    ]
    watched = [r for r in results if "machine" in r["latency_ms"]]
    if watched:
        lines += ["", conditions(watched)]
    return "\n".join(lines)


def conditions(results: list[dict[str, Any]]) -> str:
    """How the machine looked while each model was timed (machine.Watch)."""
    lines = [
        "Timing conditions (whole machine, during each model's timed queries):",
        "",
        "| Model | System CPU busy | Fastest core at its slowest (MHz) | Flags |",
        "|---|---|---|---|",
    ]
    for r in results:
        m = r["latency_ms"]["machine"]
        low = (
            "n/a"
            if m["cpu_mhz_min"] is None
            else f"{m['cpu_mhz_min']:,.0f} of {m['cpu_mhz_max']:,.0f}"
        )
        lines.append(
            f"| {r['model']} | {m['system_busy_percent']:.1f} percent | {low} | "
            f"{'; '.join(m['flags']) or 'none'} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", nargs="+", default=list(MODELS))
    parser.add_argument("--one", help=argparse.SUPPRESS)  # a child: measure this model
    parser.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--into", type=Path, help="an earlier run's directory: measure --models again into it"
    )
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    if args.one:
        result = measure(args.one, args.out)
        (args.out / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        return

    out = args.into or results_dir()
    out.mkdir(parents=True, exist_ok=args.into is not None)
    environment = describe(1)
    (out / "environment.txt").write_text(as_text(environment) + "\n", encoding="utf-8")
    for model in args.models:
        print(f"measuring {model} ...", flush=True)
        subprocess.run(
            [sys.executable, __file__, "--one", model, "--out", str(out / slug(model))],
            check=True,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
    # Every model measured into this directory, in the order of MODELS.
    results = [
        json.loads((out / slug(m) / "result.json").read_text(encoding="utf-8"))
        for m in MODELS
        if (out / slug(m) / "result.json").exists()
    ]
    chosen = winner(results)
    text = comparison(results, chosen)
    (out / "compare.md").write_text(text + "\n", encoding="utf-8")
    (out / "compare.json").write_text(
        json.dumps({"environment": environment, "results": results,
                    "winner": None if chosen is None else chosen["model"]}, indent=1) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    print(text)
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
