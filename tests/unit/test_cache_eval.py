"""B4's evaluation code (bench/cache_eval), with stand-in embedders: the
sweep's arithmetic, the threshold rule, the results' tables, the committed
data, and the chart. The real models only run in the benchmark itself."""

import json
import math
import re
import sys
from pathlib import Path

import pytest

from tests.helpers import ROOT, WordEmbedder

# The scripts import each other as siblings, as they run.
sys.path.insert(0, str(ROOT / "bench" / "cache_eval"))
from pairs import ADVERSARIAL, SAMPLE, Pair, load_adversarial
from scoring import (
    THRESHOLDS,
    EmbedMany,
    Rates,
    SweepRow,
    allowed_false_hits,
    by_category,
    choose,
    evaluate,
    false_hits_text,
    percentile,
    rates,
    recall_text,
    share,
    sweep_table,
)


def pair(a: str, b: str, duplicate: bool, category: str = "test") -> Pair:
    return Pair("x", a, b, duplicate, category)


def test_the_sweep_runs_from_080_to_099_in_hundredths() -> None:
    assert THRESHOLDS[0] == 0.80 and THRESHOLDS[-1] == 0.99 and len(THRESHOLDS) == 20


def test_false_hits_and_recall_at_a_threshold() -> None:
    pairs = [
        pair("a", "b", True),
        pair("c", "d", True),
        pair("e", "f", False),
        pair("g", "h", False),
    ]
    sims = [0.95, 0.85, 0.91, 0.70]
    r = rates(sims, pairs, 0.90)
    assert (r.hits, r.positives, r.recall) == (1, 2, 0.5)
    assert (r.false_hits, r.negatives, r.false_hit_rate) == (1, 2, 0.5)
    at_or_above = rates([0.90], [pair("a", "b", False)], 0.90)
    assert at_or_above.false_hits == 1  # "similarity >= threshold" is a hit
    assert rates([0.5], [pair("a", "b", True)], 0.9).false_hit_rate is None  # no negatives


def test_the_lowest_threshold_meeting_the_target_on_both_sets_is_chosen() -> None:
    positives = [pair(f"p{i}", f"q{i}", True) for i in range(10)]
    # QQP: one non-duplicate in 100 scores 0.93, the rest low.
    qqp = positives + [pair(f"n{i}", f"m{i}", False) for i in range(100)]
    qqp_sims = [0.97] * 10 + [0.93] + [0.5] * 99
    # Adversarial: 3 of 50 near-misses score 0.86, 0.88 and 0.95.
    adv = [pair(f"x{i}", f"y{i}", False) for i in range(50)]
    adv_sims = [0.86, 0.88, 0.95] + [0.6] * 47
    rows = [SweepRow(t, rates(qqp_sims, qqp, t), rates(adv_sims, adv, t)) for t in THRESHOLDS]
    chosen = choose(rows)
    # At 0.87 the adversarial set has 2 of 50 false hits (4%); at 0.89, 1 (2%).
    assert chosen is not None and chosen.threshold == 0.89
    assert chosen.adversarial.false_hit_rate == 0.02
    assert chosen.qqp.recall == 1.0


def embed_many(texts: list[str]) -> list[list[float]]:
    embedder = WordEmbedder()
    return [embedder.embed(t) for t in texts]


def test_evaluate_end_to_end_with_the_stand_in() -> None:
    qqp = [
        pair("what is the capital of austria", "what is the capital of austria today", True),
        pair("how do i boil an egg", "how do i boil an egg quickly", True),
        pair("who wrote hamlet", "how far is the moon", False),
    ]
    adversarial = [
        pair("what is the capital of austria", "what is the capital of australia", False, "entity"),
        pair("how do i start a container", "how do i stop a container", False, "action"),
        pair("which ocean is largest", "which ocean is largest on earth", True, "paraphrase"),
    ]
    report = evaluate(embed_many, qqp, adversarial)
    assert report.chosen is not None
    # Each near-miss changes one word of six: the stand-in scores it between
    # 0.8 and 0.9 (5/6, or a little more where two words share a bucket). The
    # chosen threshold is the lowest above both, where neither hits.
    near_misses = report.adversarial_similarities[:2]
    assert all(0.8 < s < 0.9 for s in near_misses), near_misses
    expected = min(t for t in THRESHOLDS if t > max(near_misses))
    assert report.chosen.threshold == expected
    assert report.chosen.adversarial.false_hits == 0
    assert report.chosen.qqp.recall == 1.0  # both paraphrases score 0.93
    assert report.reported is report.chosen
    categories = report.chosen.categories
    assert list(categories) == ["entity", "action", "paraphrase"]  # as they first appear
    assert categories["entity"] == categories["action"] == Rates(0, 1, 0, 0)
    assert categories["paraphrase"].positives == 1
    assert report.near_misses_hit(report.chosen.threshold) == []
    table = sweep_table(report)
    assert table.count("\n") == len(THRESHOLDS) + 1  # a header, a rule, a row each
    assert f"| {expected:.2f} **(chosen)** |" in table


def test_no_threshold_is_chosen_when_none_meets_the_target() -> None:
    # A near-miss the model can't tell apart at all: similarity 1 at every threshold.
    adversarial = [pair("what is two plus two", "WHAT is two plus two?", False, "number")]
    report = evaluate(embed_many, [], adversarial)
    assert report.chosen is None
    assert report.reported.threshold == 0.99  # the breakdown is at the sweep's highest
    assert report.reported.categories == {"number": Rates(1, 1, 0, 0)}
    assert [p.category for p, _ in report.near_misses_hit(0.99)] == ["number"]
    rows = [SweepRow(t, rates([], [], t), rates([0.5], adversarial, t)) for t in THRESHOLDS]
    assert choose(rows) is not None  # and a set with nothing to fail meets it at 0.80
    assert choose(rows).threshold == 0.80  # type: ignore[union-attr]


def test_false_hits_and_recall_by_category() -> None:
    pairs = [pair("a", "b", False, "number"), pair("c", "d", False, "number"),
             pair("e", "f", False, "unit"), pair("g", "h", True, "paraphrase"),
             pair("i", "j", True, "paraphrase")]  # fmt: skip
    assert by_category([0.95, 0.5, 0.92, 0.99, 0.7], pairs, 0.9) == {
        "number": Rates(false_hits=1, negatives=2, hits=0, positives=0),
        "unit": Rates(false_hits=1, negatives=1, hits=0, positives=0),
        "paraphrase": Rates(false_hits=0, negatives=0, hits=1, positives=2),
    }


def test_a_rate_is_shown_with_the_counts_it_comes_from() -> None:
    assert share(3, 191, 2) == "1.57% (3/191)"
    assert share(1700, 2500) == "68.0% (1,700/2,500)"
    assert share(0, 0) == "n/a"
    # False-hit rates get two decimals: 51 of 2,500 misses the 2% target, and
    # must not print as 2.0%.
    assert false_hits_text(Rates(51, 2500, 0, 0)) == "2.04% (51/2,500)"
    assert recall_text(Rates(0, 0, 40, 49)) == "81.6% (40/49)"
    assert recall_text(Rates(3, 191, 0, 0)) == "n/a"  # no positives: no recall


def test_the_false_hits_the_target_allows() -> None:
    assert allowed_false_hits(191) == 3  # a 4th is 2.09%
    assert allowed_false_hits(2500) == 50
    assert allowed_false_hits(49) == 0  # one would be 2.04%
    assert allowed_false_hits(0) == 0


def scripted(similarity: dict[str, float]) -> EmbedMany:
    """An embedder giving pair `key`, the questions "q <key>" and "r <key>",
    the similarity asked: each "q" lies on one axis, its "r" at that angle."""

    def embed_many(texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            side, key = text.split(" ", 1)
            s = 1.0 if side == "q" else similarity[key]
            out.append([s, math.sqrt(max(0.0, 1 - s * s))])
        return out

    return embed_many


RATE = re.compile(r"\d+\.\d+%")
RATE_WITH_COUNTS = re.compile(r"\d+\.\d+% \(\d[\d,]*/\d[\d,]*\)")


def test_the_results_break_down_by_category_with_counts_next_to_every_rate() -> None:
    from compare_embedders import comparison, summarize, winner
    from eval_threshold import summary

    # QQP: duplicates at 0.95 and 0.85, non-duplicates at 0.6 and 0.5.
    # Adversarial: 60 near-misses, 30 "number" and 30 "entity", one of them
    # (adv-1) at 0.97, which the 2% target allows; hard positives at 0.95, 0.7.
    sims = {"q1": 0.95, "q2": 0.85, "q3": 0.6, "q4": 0.5}
    qqp = [Pair(k, f"q {k}", f"r {k}", k in ("q1", "q2"), "qqp") for k in sims]
    adversarial = []
    for n in range(1, 63):
        key = f"adv-{n}"
        positive = n > 60
        category = "paraphrase" if positive else "number" if n <= 30 else "entity"
        adversarial.append(Pair(key, f"q {key}", f"r {key}", positive, category))
        sims[key] = 0.97 if n == 1 else 0.95 if n == 61 else 0.7 if n == 62 else 0.5
    report = evaluate(scripted(sims), qqp, adversarial)
    assert report.chosen is not None and report.chosen.threshold == 0.80

    md = summary("org/stand-in", report)
    assert "**Threshold chosen: 0.80**" in md
    assert "at most 0 of the 2 QQP non-duplicates, and 1 of the 60 near-misses" in md
    assert "| QQP | non-duplicates | 0.00% (0/2) |" in md
    assert "| Adversarial | number | 3.33% (1/30) |" in md
    assert "| Adversarial | entity | 0.00% (0/30) |" in md
    assert "| Adversarial | all near-misses | 1.67% (1/60) |" in md
    assert "| QQP | duplicates | 100.0% (2/2) |" in md
    assert "| Adversarial | paraphrase | 50.0% (1/2) |" in md
    assert "| adv-1 | number | 0.970 | q adv-1 | r adv-1 |" in md  # the near-miss that hit
    assert "| Threshold | number (30) | entity (30) |" in md

    result = summarize(
        "org/stand-in", report, dimensions=2, load_s=1.0, memory_bytes=2**20,
        timings=[1.0, 2.0], torch_threads=1,
    )  # fmt: skip
    compare = comparison([result], winner([result]))
    assert "| Pairs | stand-in at 0.80 |" in compare
    assert "| False hits: number | 3.33% (1/30) |" in compare
    assert "| False hits: all near-misses | 1.67% (1/60) |" in compare
    assert "| Recall: paraphrase | 50.0% (1/2) |" in compare
    for text in (md, compare):
        rates_shown = RATE.findall(text)
        assert rates_shown and len(rates_shown) == len(RATE_WITH_COUNTS.findall(text)), text


def test_percentiles_interpolate_between_ranks() -> None:
    values = [float(v) for v in range(1, 101)]  # 1 .. 100
    assert percentile(values, 50) == 50.5
    assert percentile(values, 99) == pytest.approx(99.01)
    assert percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


# --- the committed data ------------------------------------------------------------------

CATEGORIES = {"number", "entity", "action", "negation", "unit", "year", "comparison",
              "technical", "reversal", "scope", "paraphrase"}  # fmt: skip


def test_the_adversarial_set_is_well_formed() -> None:
    lines = [line for line in ADVERSARIAL.read_text(encoding="utf-8").splitlines() if line]
    items = [json.loads(line) for line in lines]
    assert all(set(item) == {"id", "a", "b", "duplicate", "category"} for item in items)
    assert all(re.fullmatch(r"adv-[1-9][0-9]*", item["id"]) for item in items)
    assert all(isinstance(item["duplicate"], bool) for item in items)
    assert {item["category"] for item in items} == CATEGORIES
    pairs = load_adversarial()
    assert len({p.id for p in pairs}) == len(pairs)
    near_misses = [p for p in pairs if not p.duplicate]
    positives = [p for p in pairs if p.duplicate]
    # SPEC §9: ~200 near-misses, plus ~50 hard positives.
    assert 180 <= len(near_misses) <= 220 and 40 <= len(positives) <= 60
    assert all(p.category == "paraphrase" for p in positives)
    assert all(p.category != "paraphrase" for p in near_misses)
    keys = [(p.a.lower(), p.b.lower()) for p in pairs]
    assert len(set(keys)) == len(keys)  # no pair twice
    assert all(p.a.strip().lower() != p.b.strip().lower() for p in pairs)


def test_an_adversarial_id_used_twice_is_refused(tmp_path: Path) -> None:
    item = {"id": "adv-1", "a": "What's 2+2?", "b": "What's 2+3?", "duplicate": False,
            "category": "number"}  # fmt: skip
    path = tmp_path / "pairs.jsonl"
    path.write_text(
        f"{json.dumps(item)}\n{json.dumps({**item, 'b': 'What is 2+4?'})}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="used twice"):
        load_adversarial(path)


def test_the_qqp_sample_is_ids_and_a_checksum_never_text() -> None:
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert set(sample) == {"source", "split", "sha256", "seed", "per_label", "ids"}
    assert sample["source"].startswith("https://")
    assert re.fullmatch(r"[0-9a-f]{64}", sample["sha256"])
    ids = sample["ids"]
    assert len(ids) == len(set(ids)) == 2 * sample["per_label"] == 5000
    assert all(i.isdigit() for i in ids)


def test_the_downloaded_text_is_not_committed() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "bench/cache_eval/.data/" in ignored


# --- the chart ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_chart_is_drawn_for_both_themes(tmp_path: Path, theme: str) -> None:
    pytest.importorskip("matplotlib")
    from chart import draw

    qqp = [pair("what is the capital of austria", "what is the capital of austria today", True),
           pair("who wrote hamlet", "how far is the moon", False)]  # fmt: skip
    adversarial = [pair("how do i start a container", "how do i stop a container", False, "action")]
    report = evaluate(embed_many, qqp, adversarial)
    path = tmp_path / f"chart-{theme}.png"
    draw(report, "B4: stand-in", path, theme)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
