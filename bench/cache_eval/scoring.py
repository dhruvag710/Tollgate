"""B4's arithmetic (SPEC §9): how similar each pair's two questions come out,
the false-hit rate and recall at each threshold, overall and by category, the
threshold chosen, and the tables that report them.

Pure functions over plain data: no model, no files. The scripts around them
(eval_threshold.py, compare_embedders.py) supply a real embedding model; tests
supply a stand-in.

- false-hit rate: of the pairs that must NOT share an answer, the share that
  would (similarity at or above the threshold)
- recall: of the pairs that should share an answer, the share that would
- the choice: the lowest threshold whose false-hit rate is at most 2% on both
  sets (QQP and the adversarial set), so the cache hits as often as it can
  without serving a wrong answer more than 2% of the time on either

Every rate is reported with the counts it comes from, as "1.57% (3/191)": in a
set this small, one pair moves a rate by half a point.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

THRESHOLDS = tuple(round(0.80 + 0.01 * i, 2) for i in range(20))  # 0.80, 0.81, ... 0.99
MAX_FALSE_HIT_RATE = 0.02

EmbedMany = Callable[[list[str]], list[list[float]]]


class LabelledPair(Protocol):
    """A pair of questions and whether they share an answer (pairs.Pair)."""

    @property
    def id(self) -> str: ...

    @property
    def a(self) -> str: ...

    @property
    def b(self) -> str: ...

    @property
    def duplicate(self) -> bool: ...  # True: the same question, should hit; False: must not

    @property
    def category(self) -> str: ...


def cosine(u: Sequence[float], v: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=True))
    norm = math.sqrt(sum(x * x for x in u)) * math.sqrt(sum(y * y for y in v))
    return dot / norm if norm else 0.0


def similarities(embed_many: EmbedMany, pairs: Sequence[LabelledPair]) -> list[float]:
    """Each pair's cosine similarity. Every distinct text is embedded once."""
    texts = sorted({text for p in pairs for text in (p.a, p.b)})
    vectors = dict(zip(texts, embed_many(texts), strict=True))
    return [cosine(vectors[p.a], vectors[p.b]) for p in pairs]


@dataclass(frozen=True)
class Rates:
    false_hits: int  # non-duplicates at or above the threshold
    negatives: int
    hits: int  # duplicates at or above the threshold
    positives: int

    @property
    def false_hit_rate(self) -> float | None:
        return self.false_hits / self.negatives if self.negatives else None

    @property
    def recall(self) -> float | None:
        return self.hits / self.positives if self.positives else None

    def as_json(self) -> dict[str, Any]:
        return {
            "false_hits": self.false_hits,
            "negatives": self.negatives,
            "false_hit_rate": self.false_hit_rate,
            "hits": self.hits,
            "positives": self.positives,
            "recall": self.recall,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Rates":
        return cls(data["false_hits"], data["negatives"], data["hits"], data["positives"])


def rates(sims: Sequence[float], pairs: Sequence[LabelledPair], threshold: float) -> Rates:
    false_hits = hits = negatives = positives = 0
    for sim, pair in zip(sims, pairs, strict=True):
        hit = sim >= threshold
        if pair.duplicate:
            positives += 1
            hits += hit
        else:
            negatives += 1
            false_hits += hit
    return Rates(false_hits, negatives, hits, positives)


def by_category(
    sims: Sequence[float], pairs: Sequence[LabelledPair], threshold: float
) -> dict[str, Rates]:
    """Each category's rates at the threshold, in the order the categories
    first appear."""
    groups: dict[str, tuple[list[float], list[LabelledPair]]] = {}
    for sim, pair in zip(sims, pairs, strict=True):
        group_sims, group_pairs = groups.setdefault(pair.category, ([], []))
        group_sims.append(sim)
        group_pairs.append(pair)
    return {category: rates(s, p, threshold) for category, (s, p) in groups.items()}


@dataclass(frozen=True)
class SweepRow:
    threshold: float
    qqp: Rates
    adversarial: Rates
    # The adversarial set's rates in each of its categories.
    categories: dict[str, Rates] = field(default_factory=dict)

    def meets_target(self, max_false_hit_rate: float = MAX_FALSE_HIT_RATE) -> bool:
        return all(
            r.false_hit_rate is None or r.false_hit_rate <= max_false_hit_rate
            for r in (self.qqp, self.adversarial)
        )


@dataclass(frozen=True)
class Report:
    rows: list[SweepRow]
    chosen: SweepRow | None  # None: no threshold in the sweep meets the target
    qqp: Sequence[LabelledPair] = ()
    adversarial: Sequence[LabelledPair] = ()
    qqp_similarities: list[float] = field(default_factory=list, repr=False)
    adversarial_similarities: list[float] = field(default_factory=list, repr=False)

    @property
    def reported(self) -> SweepRow:
        """The row the breakdown is reported at: the chosen threshold, or the
        sweep's highest if none was chosen."""
        if self.chosen is not None:
            return self.chosen
        return max(self.rows, key=lambda row: row.threshold)

    def near_misses_hit(self, threshold: float) -> list[tuple[LabelledPair, float]]:
        """The adversarial near-misses that would hit at the threshold, each
        with its similarity, most similar first."""
        hit = [
            (pair, sim)
            for pair, sim in zip(self.adversarial, self.adversarial_similarities, strict=True)
            if not pair.duplicate and sim >= threshold
        ]
        return sorted(hit, key=lambda h: h[1], reverse=True)


def choose(rows: Sequence[SweepRow]) -> SweepRow | None:
    """The lowest threshold meeting the target on both sets (SPEC §9)."""
    for row in sorted(rows, key=lambda r: r.threshold):
        if row.meets_target():
            return row
    return None


def allowed_false_hits(negatives: int) -> int:
    """The most false hits among this many non-duplicates that still meet the
    target: 3 of 191 at 2%, since a 4th is 2.09%."""
    return sum(1 for k in range(1, negatives + 1) if k / negatives <= MAX_FALSE_HIT_RATE)


def evaluate(
    embed_many: EmbedMany,
    qqp: Sequence[LabelledPair],
    adversarial: Sequence[LabelledPair],
    thresholds: Sequence[float] = THRESHOLDS,
) -> Report:
    qqp_sims = similarities(embed_many, qqp)
    adv_sims = similarities(embed_many, adversarial)
    return sweep(qqp, adversarial, qqp_sims, adv_sims, thresholds)


def sweep(
    qqp: Sequence[LabelledPair],
    adversarial: Sequence[LabelledPair],
    qqp_sims: list[float],
    adv_sims: list[float],
    thresholds: Sequence[float] = THRESHOLDS,
) -> Report:
    """The sweep over scores already computed, one per pair: an embedding
    model's similarities, or a two-stage check's (two_stage.py)."""
    rows = [
        SweepRow(
            t,
            rates(qqp_sims, qqp, t),
            rates(adv_sims, adversarial, t),
            by_category(adv_sims, adversarial, t),
        )
        for t in thresholds
    ]
    return Report(rows, choose(rows), qqp, adversarial, qqp_sims, adv_sims)


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile (0-100), linearly interpolated between ranks."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    rank = (len(ordered) - 1) * p / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


# --- the tables --------------------------------------------------------------------------


def share(part: int, whole: int, decimals: int = 1) -> str:
    """A rate and the counts it comes from, "1.57% (3/191)"; "n/a" of none."""
    if not whole:
        return "n/a"
    return f"{part / whole:.{decimals}%} ({part:,}/{whole:,})"


def false_hits_text(r: Rates) -> str:
    # Two decimals, so a rate just over the target (51 of 2,500 is 2.04%)
    # never prints as 2.0%.
    return share(r.false_hits, r.negatives, 2)


def recall_text(r: Rates) -> str:
    return share(r.hits, r.positives)


def _mark(report: Report, row: SweepRow) -> str:
    return " **(chosen)**" if row is report.chosen else ""


def sweep_table(report: Report) -> str:
    """The sweep as a markdown table, the chosen threshold marked."""
    lines = [
        "| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |",
        "|---|---|---|---|---|",
    ]
    for row in report.rows:
        lines.append(
            f"| {row.threshold:.2f}{_mark(report, row)} | {false_hits_text(row.qqp)} | "
            f"{recall_text(row.qqp)} | {false_hits_text(row.adversarial)} | "
            f"{recall_text(row.adversarial)} |"
        )
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def breakdown(report: Report, score: str = "Similarity") -> str:
    """False hits and recall by category where the report is read (the chosen
    threshold, else the sweep's highest), and the near-misses that hit there,
    each with its `score`."""
    row = report.reported
    at = f"{row.threshold:.2f}"
    categories = row.categories.items()
    lines = [
        f"False hits at {at}, by category (pairs that should not hit):",
        "",
        "| Set | Category | False hits |",
        "|---|---|---|",
        f"| QQP | non-duplicates | {false_hits_text(row.qqp)} |",
        *(f"| Adversarial | {c} | {false_hits_text(r)} |" for c, r in categories if r.negatives),
        f"| Adversarial | all near-misses | {false_hits_text(row.adversarial)} |",
        "",
        f"Recall at {at}, by category (pairs that should hit):",
        "",
        "| Set | Category | Recall |",
        "|---|---|---|",
        f"| QQP | duplicates | {recall_text(row.qqp)} |",
        *(f"| Adversarial | {c} | {recall_text(r)} |" for c, r in categories if r.positives),
        "",
    ]
    hit = report.near_misses_hit(row.threshold)
    if not hit:
        lines.append(f"No near-miss hits at {at}.")
        return "\n".join(lines)
    lines += [
        f"The near-misses that hit at {at}, {len(hit)} of {row.adversarial.negatives:,}:",
        "",
        f"| Pair | Category | {score} | Question | Near-miss |",
        "|---|---|---|---|---|",
        *(f"| {p.id} | {p.category} | {s:.3f} | {_cell(p.a)} | {_cell(p.b)} |" for p, s in hit),
    ]
    return "\n".join(lines)


def category_sweep_table(report: Report) -> str:
    """Near-misses at or above each threshold, by category: counts, each
    category's number of pairs in its heading."""
    first = report.rows[0].categories
    names = [c for c, r in first.items() if r.negatives]
    lines = [
        "| Threshold | " + " | ".join(f"{c} ({first[c].negatives})" for c in names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for row in report.rows:
        counts = " | ".join(str(row.categories[c].false_hits) for c in names)
        lines.append(f"| {row.threshold:.2f}{_mark(report, row)} | {counts} |")
    return "\n".join(lines)
