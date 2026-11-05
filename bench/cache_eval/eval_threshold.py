"""B4's threshold sweep for one embedding model (SPEC §9).

Embeds both questions of every pair (QQP's 5,000, then the adversarial set),
and for each threshold from 0.80 to 0.99 reports the false-hit rate and the
recall on each set, and by category; the threshold chosen is the lowest at
which false hits are at most 2% on both. Every rate comes with its counts.

    python bench/cache_eval/eval_threshold.py BAAI/bge-small-en-v1.5

Writes bench/results/b4-<UTC stamp>/<model>/: sweep.md (the sweep, false hits
and recall by category at the chosen threshold, and the near-misses that hit
there), sweep.json (every number, each pair's similarity by id, and the
machine it ran on), chart-light.png and chart-dark.png. Needs `.[bench]`.
"""

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chart import draw
from machine import as_text, describe
from pairs import load_adversarial, load_qqp
from scoring import (
    MAX_FALSE_HIT_RATE,
    Report,
    allowed_false_hits,
    breakdown,
    category_sweep_table,
    evaluate,
    sweep_table,
)

ROOT = Path(__file__).resolve().parents[2]


def results_dir() -> Path:
    return ROOT / "bench" / "results" / f"b4-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def slug(model: str) -> str:
    return model.replace("/", "__")


def as_json(report: Report) -> dict[str, Any]:
    reported = report.reported
    return {
        "sweep": [
            {"threshold": row.threshold, "qqp": row.qqp.as_json(),
             "adversarial": row.adversarial.as_json(),
             "categories": {c: r.as_json() for c, r in row.categories.items()}}
            for row in report.rows
        ],
        "chosen_threshold": None if report.chosen is None else report.chosen.threshold,
        "reported_at": reported.threshold,  # the chosen threshold, else the sweep's highest
        "near_misses_hit": [
            {"id": p.id, "category": p.category, "similarity": s}
            for p, s in report.near_misses_hit(reported.threshold)
        ],
        # Every pair's similarity, by id. QQP's ids, never its text (pairs.py).
        "qqp_pairs": [
            {"id": p.id, "duplicate": p.duplicate, "similarity": s}
            for p, s in zip(report.qqp, report.qqp_similarities, strict=True)
        ],
        "adversarial_pairs": [
            {"id": p.id, "category": p.category, "duplicate": p.duplicate, "similarity": s}
            for p, s in zip(report.adversarial, report.adversarial_similarities, strict=True)
        ],
    }  # fmt: skip


def summary(
    model: str,
    report: Report,
    *,
    heading: str | None = None,
    intro: Sequence[str] = (),
    score: str = "Similarity",
) -> str:
    """sweep.md; two_stage.py gives it a heading of its own, an intro about
    the first stage, and the name of the second stage's score."""
    qqp, adv = report.rows[0].qqp, report.rows[0].adversarial
    low, high = report.rows[0].threshold, report.rows[-1].threshold
    target = (
        f"{MAX_FALSE_HIT_RATE:.0%} on both sets: at most {allowed_false_hits(qqp.negatives):,} "
        f"of the {qqp.negatives:,} QQP non-duplicates, and {allowed_false_hits(adv.negatives):,} "
        f"of the {adv.negatives:,} near-misses"
    )
    lines = [
        heading or f"### B4 sweep: `{model}`",
        "",
        f"QQP: {qqp.positives:,} duplicates, {qqp.negatives:,} not; adversarial: "
        f"{adv.negatives:,} near-misses, {adv.positives:,} hard positives.",
        "",
        *(line for paragraph in intro for line in (paragraph, "")),
    ]
    if report.chosen is None:
        lines.append(
            f"**No threshold from {low:.2f} to {high:.2f} keeps false hits at or below {target}.** "
            f"The breakdown is at {report.reported.threshold:.2f}, the sweep's highest."
        )
    else:
        lines.append(
            f"**Threshold chosen: {report.chosen.threshold:.2f}**, the lowest with false hits "
            f"at or below {target}."
        )
    lines += [
        "",
        sweep_table(report),
        "",
        breakdown(report, score),
        "",
        "Near-misses at or above each threshold, by category (each category's pairs in brackets):",
        "",
        category_sweep_table(report),
    ]
    return "\n".join(lines)


def write(
    model: str, report: Report, out: Path, environment: dict[str, Any], extra: dict[str, Any]
) -> str:
    """The sweep's files for one model under `out`; returns its markdown."""
    out.mkdir(parents=True, exist_ok=True)
    qqp, adv = report.rows[0].qqp, report.rows[0].adversarial
    counts = {
        "qqp_duplicates": qqp.positives,
        "qqp_non_duplicates": qqp.negatives,
        "adversarial_near_misses": adv.negatives,
        "adversarial_positives": adv.positives,
    }
    text = summary(model, report)
    (out / "sweep.md").write_text(text + "\n", encoding="utf-8")
    (out / "environment.txt").write_text(as_text(environment) + "\n", encoding="utf-8")
    record = {"model": model, "counts": counts, "environment": environment, **extra}
    (out / "sweep.json").write_text(
        json.dumps({**record, **as_json(report)}, indent=1) + "\n", encoding="utf-8"
    )
    for theme in ("light", "dark"):
        draw(report, f"B4: {model}", out / f"chart-{theme}.png", theme)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("model")
    parser.add_argument("--out", type=Path, help="default: bench/results/b4-<stamp>/<model>")
    args = parser.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # load what's downloaded; time no network
    import psutil

    from tollgate.cache.embedder import SentenceTransformerEmbedder

    threads = psutil.cpu_count(logical=False) or 1  # quality only here: use every core
    embedder = SentenceTransformerEmbedder(args.model, threads=threads)
    report = evaluate(embedder.embed_many, load_qqp(), load_adversarial())
    out = args.out or results_dir() / slug(args.model)
    print(write(args.model, report, out, describe(threads), {}))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
