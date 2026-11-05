"""B4's second stage (bench/cache_eval/two_stage.py), with stand-ins: the loose
threshold, the gate, mutual entailment, the cross-encoder wrapper, and the
reports. The real cross-encoders only run in the benchmark itself."""

import re
import sys
import types
from typing import Any

import pytest

from tests.helpers import ROOT

sys.path.insert(0, str(ROOT / "bench" / "cache_eval"))
from pairs import Pair
from scoring import Rates
from two_stage import (
    MISS_PATH_BUDGET_MS,
    Verifier,
    comparison,
    gated,
    line,
    loose_threshold,
    mutual_entailment,
    pairing,
    stage1_intro,
    winner,
)

NLI = "cross-encoder/nli-deberta-v3-xsmall"
STS = "cross-encoder/stsb-distilroberta-base"


def test_the_loose_threshold_keeps_95_percent_of_qqp_duplicates() -> None:
    qqp = [Pair(str(i), "a", "b", i < 20, "qqp") for i in range(25)]
    sims = [0.9] * 19 + [0.5] + [0.99] * 5  # duplicates: 19 at 0.9, one at 0.5
    assert loose_threshold(qqp, sims) == 0.90  # 19 of 20 kept; at 0.91, none
    assert loose_threshold(qqp, [0.1] * 25) == 0.30  # never 95%: the grid's lowest


def test_the_gate_passes_only_what_stage_one_passed() -> None:
    assert gated([0.9, 0.8, 0.7], [0.85, 0.79, 0.80], 0.80) == [0.9, 0.0, 0.7]


def test_a_match_needs_entailment_both_ways() -> None:
    # Labels as the NLI cross-encoders order them: contradiction, entailment, neutral.
    forward, backward = [[0.1, 0.8, 0.1], [0.0, 0.9, 0.1]], [[0.6, 0.3, 0.1], [0.0, 0.95, 0.05]]
    assert mutual_entailment(forward, backward, 1) == [0.3, 0.9]


class StandInCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder."""

    def __init__(self, name: str, device: str) -> None:
        self.name, self.device, self.calls = name, device, []
        labels = (
            {0: "contradiction", 1: "entailment", 2: "neutral"} if "nli" in name else {0: "LABEL_0"}
        )
        self.model = types.SimpleNamespace(
            config=types.SimpleNamespace(id2label=labels), float=lambda: None
        )

    def predict(self, pairs: list[tuple[str, str]], **options: Any) -> list[Any]:
        self.calls.append((list(pairs), options))
        if "nli" in self.name:  # A entails B strongly, B entails A weakly
            return [[0.0, 0.9, 0.1] if a == "A" else [0.7, 0.2, 0.1] for a, _ in pairs]
        return [0.7 for _ in pairs]


@pytest.fixture
def cross_encoders(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    made: dict[str, Any] = {}

    def make(name: str, device: str) -> StandInCrossEncoder:
        made[name] = StandInCrossEncoder(name, device)
        return made[name]

    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda n: None  # type: ignore[attr-defined]
    st = types.ModuleType("sentence_transformers")
    st.CrossEncoder = make  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    return made


def test_the_nli_check_asks_both_directions_and_keeps_the_lesser(
    cross_encoders: dict[str, Any],
) -> None:
    verifier = Verifier(NLI)
    assert verifier.score_many([("A", "B")]) == [0.2]
    calls = cross_encoders[NLI].calls
    assert [c[0] for c in calls] == [[("A", "B")], [("B", "A")]]
    assert all(c[1]["apply_softmax"] for c in calls)
    assert verifier.score("A", "B") == 0.2
    assert calls[-1][0] == [("A", "B"), ("B", "A")]  # one call, both directions


def test_the_sts_check_scores_the_cached_question_first(cross_encoders: dict[str, Any]) -> None:
    verifier = Verifier(STS)
    assert verifier.score_many([("A", "B")]) == [0.7]
    assert verifier.score("A", "B") == 0.7
    assert [c[0] for c in cross_encoders[STS].calls] == [[("A", "B")], [("A", "B")]]


RATE = re.compile(r"\d+\.\d+%")
RATE_WITH_COUNTS = re.compile(r"\d+\.\d+% \(\d[\d,]*/\d[\d,]*\)")


def machine() -> dict[str, Any]:
    return {"system_busy_percent": 13.0, "cpu_mhz_min": 2701, "cpu_mhz_max": 2701,
            "on_battery": False, "flags": []}  # fmt: skip


def test_two_stages_end_to_end_with_counts_next_to_every_rate() -> None:
    # QQP: 20 duplicates (similarity 0.9, the cross-encoder agrees at 0.9) and
    # 20 non-duplicates (similarity 0.95, all past stage 1; the cross-encoder
    # rejects them at 0.1). Adversarial: 50 near-misses at similarity 0.99
    # (cross 0.2, one at 0.8) and a hard positive at 0.4, below the loose
    # threshold, so stage 1 loses it whatever the cross-encoder says.
    qqp = [Pair(f"q{i}", f"a{i}", f"b{i}", i < 20, "qqp") for i in range(40)]
    adversarial = [Pair(f"adv-{n}", f"a{n}", f"b{n}", n == 51, "paraphrase" if n == 51 else
                        "reversal") for n in range(1, 52)]  # fmt: skip
    sims = {p.id: 0.9 if p.duplicate else 0.95 for p in qqp}
    sims |= {p.id: 0.4 if p.duplicate else 0.99 for p in adversarial}
    scores = {p.id: 0.9 if p.duplicate else 0.1 for p in qqp}
    scores |= {p.id: 0.8 if p.id == "adv-1" else 0.2 if not p.duplicate else 0.99
               for p in adversarial}  # fmt: skip
    report, loose, stage1_qqp, stage1_adv = pairing(qqp, adversarial, sims, scores)
    assert loose == 0.90
    assert stage1_adv == Rates(false_hits=50, negatives=50, hits=0, positives=1)
    assert report.chosen is not None and report.chosen.threshold == 0.25  # 1 of 50 near-misses
    assert report.chosen.qqp == Rates(0, 20, 20, 20)
    assert report.chosen.adversarial == Rates(1, 50, 0, 1)  # the hard positive, lost in stage 1
    intro = stage1_intro(loose, stage1_qqp, stage1_adv)
    assert "hard-positive recall 0.0% (0/1)" in intro and "100.00% (50/50) of near-misses" in intro

    hit_path = {"org/fast": {"p50": 9.0, "p99": 14.0, "machine": machine()}}
    verifier = {"model": NLI, "trained_on": "SNLI, MultiNLI", "load_s": 1.0, "memory_bytes": 2**20,
                "scoring_s": 3.0, "verify_ms": {"p50": 5.0, "p99": 9.0, "machine": machine()},
                "hit_path_ms": hit_path}  # fmt: skip
    fast = line("org/fast", verifier, report, loose, stage1_qqp, stage1_adv, {"p99": 12.0})
    slow = line("org/slow", verifier, report, loose, stage1_qqp, stage1_adv,
                {"p99": MISS_PATH_BUDGET_MS + 1})  # fmt: skip
    assert fast["meets_targets"] and not slow["meets_targets"]  # embedding alone over budget
    assert winner([slow, fast]) is fast
    text = comparison([fast, slow], [verifier])
    assert "| **fast** | nli-deberta-v3-xsmall | 0.90 | 0.0% (0/1) | 0.25 |" in text
    assert "| False hits: reversal | 2.00% (1/50) | 2.00% (1/50) |" in text
    assert "Timing runs flagged (machine.Watch): none." in text
    for shown in (intro, text):
        rates_shown = RATE.findall(shown)
        assert rates_shown and len(rates_shown) == len(RATE_WITH_COUNTS.findall(shown)), shown
