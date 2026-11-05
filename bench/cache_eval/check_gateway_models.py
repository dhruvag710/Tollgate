"""Before B5: do the gateway's own model wrappers, in the semantic gateway
image, give the numbers B4 measured? B4 ran its models through bench code on
the host's Python; the gateway runs them through tollgate.cache.embedder and
tollgate.cache.verifier, at pinned revisions, in a Linux image with its own
builds of the same library versions. If they disagree, B4's thresholds don't
carry over.

    docker run --rm -v ./bench:/bench:ro tollgate-gateway-semantic \\
        python /bench/cache_eval/check_gateway_models.py /bench/results/b4-<stamp> [--out FILE]

Scores every adversarial pair, and the first QQP_PAIRS of B4's QQP sample if
its download is there (bench/cache_eval/.data/), with the gateway's wrappers,
one pair at a time as the gateway does, and compares each with B4's: the
embedding model's similarity (its sweep.json) and the NLI check's score
(scores.json). Reports the largest difference of each, and every pair whose
decision at the gateway's thresholds would differ from B4's.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pairs import load_adversarial, load_qqp  # noqa: E402

from tollgate.cache.embedder import SentenceTransformerEmbedder  # noqa: E402
from tollgate.cache.verifier import NLIVerifier  # noqa: E402

QQP_PAIRS = 260  # with the 240 adversarial ones, 500 pairs: under a minute


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("b4", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    model = os.environ["SEMANTIC_CACHE_MODEL"]
    verifier_name = os.environ["SEMANTIC_CACHE_VERIFIER"]
    threshold = float(os.environ["SEMANTIC_CACHE_THRESHOLD"])
    verifier_threshold = float(os.environ["SEMANTIC_CACHE_VERIFIER_THRESHOLD"])
    slug = model.replace("/", "__")
    sweep = json.loads((args.b4 / slug / "sweep.json").read_text(encoding="utf-8"))
    b4_sims = {p["id"]: p["similarity"] for p in sweep["qqp_pairs"] + sweep["adversarial_pairs"]}
    scores_path = args.b4 / "two-stage" / verifier_name.replace("/", "__") / "scores.json"
    b4_scores = json.loads(scores_path.read_text(encoding="utf-8"))["scores"]

    pairs = list(load_adversarial())
    try:
        pairs += load_qqp()[:QQP_PAIRS]
    except (OSError, RuntimeError) as exc:  # not downloaded here: the adversarial set alone
        print(f"QQP left out: {type(exc).__name__}", file=sys.stderr)
    embedder = SentenceTransformerEmbedder(
        model, revision=os.environ.get("SEMANTIC_CACHE_MODEL_REVISION"), threads=1
    )
    verifier = NLIVerifier(
        verifier_name, revision=os.environ.get("SEMANTIC_CACHE_VERIFIER_REVISION"), threads=1
    )
    rows: list[dict[str, Any]] = []
    for p in pairs:
        a, b = embedder.embed(p.a), embedder.embed(p.b)
        similarity = sum(x * y for x, y in zip(a, b, strict=True))
        score = verifier.score(p.a, p.b)
        rows.append(
            {
                "id": p.id,
                "similarity": similarity,
                "score": score,
                "b4_similarity": b4_sims[p.id],
                "b4_score": b4_scores[p.id],
            }
        )

    def decision(sim: float, score: float) -> bool:
        return sim >= threshold and score >= verifier_threshold

    flips = [
        r["id"]
        for r in rows
        if decision(r["similarity"], r["score"]) != decision(r["b4_similarity"], r["b4_score"])
    ]
    result = {
        "pairs": len(rows),
        "model": model,
        "verifier": verifier_name,
        "max_similarity_difference": max(abs(r["similarity"] - r["b4_similarity"]) for r in rows),
        "max_score_difference": max(abs(r["score"] - r["b4_score"]) for r in rows),
        "decisions_that_differ": flips,
        "thresholds": {"similarity": threshold, "score": verifier_threshold},
        "all_finite": all(
            math.isfinite(r["similarity"]) and math.isfinite(r["score"]) for r in rows
        ),
    }
    text = json.dumps(result, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
