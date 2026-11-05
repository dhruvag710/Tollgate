"""B4's second stage (SPEC §9): an embedding model finds the nearest entry at a
loose threshold, then a cross-encoder, reading both questions together,
decides whether they are the same question.

    python bench/cache_eval/two_stage.py bench/results/b4-<stamp>

The first stage is exactly what that B4 run measured: each embedding model's
similarities are read from its sweep.json. Each cross-encoder runs in a
process of its own: it scores every pair (on every core), then is timed on
one PyTorch thread, verifying alone and on the hit path (embed the query,
then verify it) with each embedding model. Then, for each pairing of an
embedding model and a cross-encoder:

- the loose threshold: the highest from 0.30 to 0.99 at which the embedding
  model alone still keeps 95% of QQP's duplicates; hard-positive recall is
  reported there, to show what the first stage loses
- the cross-encoder's threshold: the lowest at which false hits are at most
  2% on both sets, as B4 chooses (scoring.choose)

The cross-encoders, neither trained on QQP (their model cards):
- cross-encoder/nli-deberta-v3-xsmall, trained on SNLI and MultiNLI: a match
  only if each question entails the other, so its score is the lesser of the
  two entailment probabilities
- cross-encoder/stsb-distilroberta-base, trained on the STS benchmark: its
  similarity score, the cached question first

Targets (SPEC §9): false hits at most 2% on both sets, and a miss path
(embed, then the Postgres lookup) under 25 ms at p99. This measures the
embedding share of the miss path; B5 times the lookup. The hit path has no
target: a hit replaces a provider call that takes seconds.

Writes <B4 run>/two-stage/: compare.md and compare.json, and a directory per
pairing with its sweep and breakdown. A flagged timing run (machine.Watch) is
rerun with `--verifiers <model>`. Needs `.[bench]`.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from compare_embedders import MODELS, RECALL_TIE, category_table
from eval_threshold import slug, summary
from machine import Watch, as_text, describe
from pairs import Pair, load_adversarial, load_qqp
from scoring import (
    MAX_FALSE_HIT_RATE,
    LabelledPair,
    Rates,
    Report,
    false_hits_text,
    percentile,
    rates,
    recall_text,
    share,
    sweep,
)

VERIFIERS = {
    "cross-encoder/nli-deberta-v3-xsmall": ("nli", "SNLI, MultiNLI"),
    "cross-encoder/stsb-distilroberta-base": ("sts", "STS benchmark"),
}
CROSS_THRESHOLDS = (*(round(0.05 * i, 2) for i in range(1, 20)), 0.96, 0.97, 0.98, 0.99)
LOOSE_GRID = tuple(round(0.30 + 0.01 * i, 2) for i in range(70))  # 0.30 .. 0.99
STAGE1_RECALL = 0.95
MISS_PATH_BUDGET_MS = 25.0
WARM_UP = 20
LATENCY_QUERIES = 500


def short(model: str) -> str:
    return model.split("/")[-1]


# --- the arithmetic ----------------------------------------------------------------------


def loose_threshold(qqp: Sequence[LabelledPair], qqp_sims: Sequence[float]) -> float:
    """The highest threshold in LOOSE_GRID at which the embedding model alone
    keeps STAGE1_RECALL of QQP's duplicates (the grid's lowest if none does)."""
    duplicates = [s for p, s in zip(qqp, qqp_sims, strict=True) if p.duplicate]
    loose = LOOSE_GRID[0]
    for t in LOOSE_GRID:
        if sum(s >= t for s in duplicates) >= STAGE1_RECALL * len(duplicates):
            loose = t
    return loose


def gated(scores: Sequence[float], sims: Sequence[float], loose: float) -> list[float]:
    """The second stage's score where the first passed the pair; 0 (below
    every threshold) where it didn't."""
    return [c if s >= loose else 0.0 for c, s in zip(scores, sims, strict=True)]


def mutual_entailment(
    forward: Sequence[Sequence[float]], backward: Sequence[Sequence[float]], entailment: int
) -> list[float]:
    """Each pair's lesser entailment probability: A entails B, and B entails A."""
    return [min(f[entailment], b[entailment]) for f, b in zip(forward, backward, strict=True)]


def pairing(
    qqp: Sequence[Pair],
    adversarial: Sequence[Pair],
    sims: dict[str, float],
    scores: dict[str, float],
) -> tuple[Report, float, Rates, Rates]:
    """One embedding model's first stage and one cross-encoder's second: the
    report over the cross-encoder's thresholds, the loose threshold, and what
    the first stage alone passes on QQP and the adversarial set."""
    qqp_sims = [sims[p.id] for p in qqp]
    adv_sims = [sims[p.id] for p in adversarial]
    loose = loose_threshold(qqp, qqp_sims)
    report = sweep(
        qqp,
        adversarial,
        gated([scores[p.id] for p in qqp], qqp_sims, loose),
        gated([scores[p.id] for p in adversarial], adv_sims, loose),
        CROSS_THRESHOLDS,
    )
    return report, loose, rates(qqp_sims, qqp, loose), rates(adv_sims, adversarial, loose)


# --- the cross-encoders ------------------------------------------------------------------


class Verifier:
    """A cross-encoder on the CPU, in float32, scoring whether two questions
    are the same one (VERIFIERS says how)."""

    def __init__(self, model_name: str, *, threads: int = 1) -> None:
        import torch
        from sentence_transformers import CrossEncoder

        torch.set_num_threads(threads)
        self.model_name = model_name
        self.kind = VERIFIERS[model_name][0]
        self._model: Any = CrossEncoder(model_name, device="cpu")
        self._model.model.float()
        labels = {v.lower(): k for k, v in self._model.model.config.id2label.items()}
        self._entailment = labels.get("entailment", -1)
        if self.kind == "nli" and self._entailment < 0:
            raise ValueError(f"{model_name} has no entailment label: {labels}")

    def score_many(self, pairs: list[tuple[str, str]], batch_size: int = 32) -> list[float]:
        def predict(batch: list[tuple[str, str]]) -> Any:
            return self._model.predict(
                batch, batch_size=batch_size, apply_softmax=self.kind == "nli",
                show_progress_bar=False, convert_to_numpy=True,
            )  # fmt: skip

        if self.kind == "nli":
            forward = predict(pairs)
            backward = predict([(b, a) for a, b in pairs])
            return [float(s) for s in mutual_entailment(forward, backward, self._entailment)]
        return [float(s) for s in predict(pairs)]

    def score(self, cached: str, query: str) -> float:
        """One check, as the gateway would make it: both directions of an NLI
        model in one call."""
        if self.kind == "nli":
            both = self._model.predict(
                [(cached, query), (query, cached)], apply_softmax=True,
                show_progress_bar=False, convert_to_numpy=True,
            )  # fmt: skip
            return float(min(both[0][self._entailment], both[1][self._entailment]))
        return float(self._model.predict([(cached, query)], show_progress_bar=False)[0])


def timed(call: Any, items: Sequence[Pair]) -> dict[str, Any]:
    """Each item through `call`, one at a time, after a warm-up: p50 and p99
    in ms, and how the machine looked meanwhile."""
    for p in items[:WARM_UP]:
        call(p)
    timings = []
    with Watch() as watch:
        for i, p in enumerate(items[WARM_UP:]):
            t = time.perf_counter()
            call(p)
            timings.append((time.perf_counter() - t) * 1000)
            if i % 25 == 0:
                watch.sample()
    return {
        "p50": percentile(timings, 50),
        "p99": percentile(timings, 99),
        "queries": len(timings),
        "machine": watch.result(),
    }


def measure(model: str, b4: Path, out: Path, *, reuse_scores: bool = False) -> dict[str, Any]:
    """One cross-encoder, in this process: every pair's score, memory, and
    latency alone and on each embedding model's hit path. With `reuse_scores`,
    the scores of an earlier run are kept (they don't change from run to run)
    and only the timing is done again."""
    import psutil
    import sentence_transformers  # noqa: F401  # imported before the baseline: not the model's cost
    import torch

    from tollgate.cache.embedder import SentenceTransformerEmbedder

    out.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    before = process.memory_info().rss
    started = time.perf_counter()
    verifier = Verifier(model, threads=psutil.cpu_count(logical=False) or 1)
    load_s = time.perf_counter() - started
    memory = process.memory_info().rss - before

    qqp, adversarial = load_qqp(), load_adversarial()
    pairs = [*qqp, *adversarial]
    earlier = out / "result.json"
    if reuse_scores and (out / "scores.json").exists() and earlier.exists():
        scoring_s = json.loads(earlier.read_text(encoding="utf-8"))["scoring_s"]
    else:
        started = time.perf_counter()
        scores = verifier.score_many([(p.a, p.b) for p in pairs])
        scoring_s = time.perf_counter() - started
        by_id = {p.id: s for p, s in zip(pairs, scores, strict=True)}  # QQP's ids, not its text
        (out / "scores.json").write_text(
            json.dumps({"model": model, "scores": by_id}), encoding="utf-8"
        )

    torch.set_num_threads(1)  # as the gateway would run it
    sample = qqp[: WARM_UP + LATENCY_QUERIES]
    verify = timed(lambda p: verifier.score(p.a, p.b), sample)
    hit_path = {}
    for bi in MODELS:
        if not (b4 / slug(bi) / "sweep.json").exists():
            continue
        embedder = SentenceTransformerEmbedder(bi, threads=1)

        def hit(p: Pair, embedder: SentenceTransformerEmbedder = embedder) -> None:
            embedder.embed(p.b)  # the query; the cached question's embedding is stored
            verifier.score(p.a, p.b)

        hit_path[bi] = timed(hit, sample)
        del embedder
    return {
        "model": model,
        "kind": VERIFIERS[model][0],
        "trained_on": VERIFIERS[model][1],
        "load_s": load_s,
        "memory_bytes": memory,
        "scoring_s": scoring_s,
        "torch_threads": torch.get_num_threads(),
        "verify_ms": verify,
        "hit_path_ms": hit_path,
    }


# --- the report --------------------------------------------------------------------------


def stage1_intro(loose: float, stage1_qqp: Rates, stage1_adv: Rates) -> str:
    return (
        f"Stage 1, the embedding model at its loose threshold {loose:.2f} (the highest that "
        f"keeps at least {STAGE1_RECALL:.0%} of QQP duplicates): QQP recall "
        f"{recall_text(stage1_qqp)}, hard-positive recall {recall_text(stage1_adv)}. It passes "
        f"{false_hits_text(stage1_qqp)} of QQP non-duplicates and "
        f"{false_hits_text(stage1_adv)} of near-misses on to the cross-encoder; a request "
        "whose candidate the cross-encoder rejects pays for the check and then misses."
    )


def line(
    bi: str, verifier: dict[str, Any], report: Report, loose: float,
    stage1_qqp: Rates, stage1_adv: Rates, embed: dict[str, Any],
) -> dict[str, Any]:  # fmt: skip
    """One pairing's line in compare.json: its rates at its chosen threshold
    (the sweep's highest if none was chosen), and its latency."""
    row = report.reported
    hit = verifier["hit_path_ms"].get(bi)
    return {
        "model": f"{short(bi)} + {short(verifier['model'])}",
        "embedding_model": bi,
        "cross_encoder": verifier["model"],
        "loose_threshold": loose,
        "stage1_qqp": stage1_qqp.as_json(),
        "stage1_adversarial": stage1_adv.as_json(),
        "chosen_threshold": None if report.chosen is None else report.chosen.threshold,
        "reported_at": row.threshold,
        "qqp": row.qqp.as_json(),
        "adversarial": row.adversarial.as_json(),
        "categories": {c: r.as_json() for c, r in row.categories.items()},
        "embed_p99_ms": embed["p99"],
        "hit_path_ms": None if hit is None else {"p50": hit["p50"], "p99": hit["p99"]},
        "meets_targets": report.chosen is not None and embed["p99"] < MISS_PATH_BUDGET_MS,
    }


def winner(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Of the pairings meeting both targets, the highest QQP recall; within
    RECALL_TIE of it, the fastest hit path at p99."""
    eligible = [x for x in lines if x["meets_targets"]]
    if not eligible:
        return None
    best = max(x["qqp"]["recall"] for x in eligible)
    close = [x for x in eligible if x["qqp"]["recall"] >= best - RECALL_TIE]
    return min(close, key=lambda x: (x["hit_path_ms"] or {"p99": float("inf")})["p99"])


def comparison(lines: list[dict[str, Any]], verifiers: list[dict[str, Any]]) -> str:
    """two-stage/compare.md."""
    chosen = winner(lines)
    table = [
        "| Embedding model | Cross-encoder | Loose threshold | Stage-1 hard-positive recall | "
        "Cross-encoder threshold | QQP false hits | Near-miss false hits | QQP recall | "
        "Hard-positive recall | Embed p99 (ms) | Hit path p50 / p99 (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for x in lines:
        qqp, adv = Rates.from_json(x["qqp"]), Rates.from_json(x["adversarial"])
        if x["chosen_threshold"] is None:
            threshold, cells = f"none meets {MAX_FALSE_HIT_RATE:.0%}", ["n/a"] * 4
        else:
            threshold = f"{x['chosen_threshold']:.2f}"
            cells = [false_hits_text(qqp), false_hits_text(adv), recall_text(qqp),
                     recall_text(adv)]  # fmt: skip
        hit = x["hit_path_ms"]
        hit_text = "n/a" if hit is None else f"{hit['p50']:.1f} / {hit['p99']:.1f}"
        name = f"**{short(x['embedding_model'])}**" if x is chosen else short(x["embedding_model"])
        table.append(
            f"| {name} | {short(x['cross_encoder'])} | {x['loose_threshold']:.2f} | "
            f"{recall_text(Rates.from_json(x['stage1_adversarial']))} | {threshold} | "
            f"{' | '.join(cells)} | {x['embed_p99_ms']:.1f} | {hit_text} |"
        )
    models = [
        "| Cross-encoder | Trained on | Verify p50 / p99 (ms) | Memory (MB) | Load (s) | "
        "Scoring every pair (s) |",
        "|---|---|---|---|---|---|",
        *(
            f"| {v['model']} | {v['trained_on']} | {v['verify_ms']['p50']:.1f} / "
            f"{v['verify_ms']['p99']:.1f} | {v['memory_bytes'] / 2**20:.0f} | {v['load_s']:.1f} | "
            f"{v['scoring_s']:.0f} |"
            for v in verifiers
        ),
    ]
    verdict = (
        "No pairing meets both targets: false hits at or below "
        f"{MAX_FALSE_HIT_RATE:.0%} on both sets, and embedding under {MISS_PATH_BUDGET_MS:.0f} ms "
        "at p99."
        if chosen is None
        else f"Best pairing meeting both targets: `{chosen['embedding_model']}` then "
        f"`{chosen['cross_encoder']}` at {chosen['chosen_threshold']:.2f}: the highest QQP recall, "
        f"or within {RECALL_TIE * 100:.0f} percentage point of it, the fastest hit path."
    )
    text = [
        "### B4: two stages, an embedding model then a cross-encoder",
        "",
        f"Stage 1 passes a pair at the embedding model's loose threshold, the highest that keeps "
        f"at least {STAGE1_RECALL:.0%} of QQP duplicates; stage 2 hits at the cross-encoder's "
        f"threshold, the lowest with false hits at or below {MAX_FALSE_HIT_RATE:.0%} on both "
        "sets. Embed p99 is the miss path's share before the Postgres lookup (budget "
        f"{MISS_PATH_BUDGET_MS:.0f} ms for both; B5 times the lookup). The hit path is embed then "
        f"verify, {LATENCY_QUERIES} queries on one PyTorch thread after {WARM_UP} to warm up.",
        "",
        *table,
        "",
        verdict,
        "",
        *models,
        "",
        "False hits and recall by category, at each pairing's chosen threshold:",
        "",
        category_table(lines),
    ]
    flagged = [
        f"- {v['model']}, {what}: {'; '.join(m['flags'])}"
        for v in verifiers
        for what, m in [("verifying", v["verify_ms"]["machine"]),
                        *((f"hit path with {short(b)}", h["machine"])
                          for b, h in v["hit_path_ms"].items())]
        if m["flags"]
    ]  # fmt: skip
    text += ["", "Timing runs flagged (machine.Watch): " + ("none." if not flagged else "")]
    text += flagged
    return "\n".join(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("b4", type=Path, help="a B4 run's directory (compare_embedders.py)")
    parser.add_argument("--verifiers", nargs="+", default=list(VERIFIERS))
    parser.add_argument("--one", help=argparse.SUPPRESS)  # a child: measure this cross-encoder
    parser.add_argument(
        "--reuse-scores", action="store_true", help="keep an earlier run's scores; time again"
    )
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    out = args.b4 / "two-stage"
    if args.one:
        result = measure(args.one, args.b4, out / slug(args.one), reuse_scores=args.reuse_scores)
        (out / slug(args.one) / "result.json").write_text(
            json.dumps(result, indent=1), encoding="utf-8"
        )
        return

    out.mkdir(exist_ok=True)
    (out / "environment.txt").write_text(as_text(describe(1)) + "\n", encoding="utf-8")
    for model in args.verifiers:
        print(f"measuring {model} ...", flush=True)
        subprocess.run(
            [
                sys.executable,
                __file__,
                str(args.b4),
                "--one",
                model,
                *(["--reuse-scores"] if args.reuse_scores else []),
            ],
            check=True,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )

    qqp, adversarial = load_qqp(), load_adversarial()
    verifiers = [
        json.loads((out / slug(v) / "result.json").read_text(encoding="utf-8"))
        for v in VERIFIERS
        if (out / slug(v) / "result.json").exists()
    ]
    lines = []
    for verifier in verifiers:
        scores = json.loads(
            (out / slug(verifier["model"]) / "scores.json").read_text(encoding="utf-8")
        )["scores"]
        for bi in MODELS:
            if not (args.b4 / slug(bi) / "sweep.json").exists():
                continue
            b4 = json.loads((args.b4 / slug(bi) / "sweep.json").read_text(encoding="utf-8"))
            sims = {r["id"]: r["similarity"] for r in b4["qqp_pairs"] + b4["adversarial_pairs"]}
            report, loose, stage1_qqp, stage1_adv = pairing(qqp, adversarial, sims, scores)
            name = f"{short(bi)} + {short(verifier['model'])}"
            text = summary(
                name, report, heading=f"### B4 two stages: `{bi}` then `{verifier['model']}`",
                intro=[stage1_intro(loose, stage1_qqp, stage1_adv)], score="Score",
            )  # fmt: skip
            pairing_out = out / f"{slug(bi)}__{slug(verifier['model'])}"
            pairing_out.mkdir(exist_ok=True)
            (pairing_out / "sweep.md").write_text(text + "\n", encoding="utf-8")
            embed = b4["comparison"]["latency_ms"]
            x = line(bi, verifier, report, loose, stage1_qqp, stage1_adv, embed)
            (pairing_out / "sweep.json").write_text(json.dumps(x, indent=1) + "\n", "utf-8")
            lines.append(x)
    text = comparison(lines, verifiers)
    (out / "compare.md").write_text(text + "\n", encoding="utf-8")
    best = winner(lines)
    (out / "compare.json").write_text(
        json.dumps({"pairings": lines, "cross_encoders": verifiers,
                    "winner": None if best is None else best["model"]}, indent=1) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    print(text)
    print(f"\nwritten to {out}")
    meeting = sum(x["meets_targets"] for x in lines)
    print(share(meeting, len(lines)), "of pairings meet both targets")


if __name__ == "__main__":
    main()
