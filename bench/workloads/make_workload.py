"""B5's workload (SPEC §9): a 20,000-request stream of questions for the
response cache, built from three sources.

    python bench/workloads/make_workload.py           # draws the stream: run once
    python bench/workloads/make_workload.py --text    # rebuilds its text from the ids

- **Base queries:** MS MARCO's (the passage-ranking train split), drawn with a
  Zipf distribution (s = 1.1) over 20,000 of them in a random order, so a few
  popular questions repeat often and most come once, like real traffic. A
  request whose query was asked before is a repeat.
- **Paraphrases:** an MS MARCO query has no QQP paraphrase, so these come as
  QQP duplicate pairs of their own: the first question is asked once, and its
  duplicate later, at least MIN_GAP requests on. The duplicate is a repeat in
  meaning, worded differently; there are as many as make the paraphrases 15% of
  all repeats. They are from QQP's train split, and none shares a question with
  B4's sample (from the dev split), so nothing B4 chose its thresholds on comes
  back here.
- **Adversarial near-misses:** 5% of the requests, from B4's 191 near-miss pairs:
  each pair's first question, then its partner at least MIN_GAP requests later,
  which must not be answered with the first one's answer; the remaining
  requests repeat one of those questions word for word.

Neither MS MARCO's nor Quora's text is committed (their terms don't clearly
allow redistributing it): bench/workloads/b5_workload.json holds each
request's source and id, and the checksums of the downloads they come from.
--text downloads MS MARCO's queries into bench/workloads/.data/ (QQP's archive
is B4's, in bench/cache_eval/.data/), checks both, and writes the stream with
its text to bench/workloads/.data/b5_requests.json for the benchmark to send.
"""

import argparse
import hashlib
import io
import json
import random
import sys
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cache_eval"))

from pairs import QQP_URL, SAMPLE, load_adversarial, qqp_archive, qqp_rows  # noqa: E402

DATA = HERE / ".data"
WORKLOAD = HERE / "b5_workload.json"
REQUESTS = DATA / "b5_requests.json"
MSMARCO_URL = "https://msmarco.z22.web.core.windows.net/msmarcoranking/queries.tar.gz"
MSMARCO_SPLIT = "queries.train.tsv"
QQP_TRAIN = "QQP/train.tsv"

SEED = 20261105
REQUESTS_TOTAL = 20_000
ZIPF_S = 1.1
CANDIDATES = 20_000  # MS MARCO queries the Zipf draws from
PARAPHRASE_SHARE = 0.15  # of all repeats
ADVERSARIAL_SHARE = 0.05  # of all requests
MIN_GAP = 100  # requests between a question and its paraphrase or near-miss
MAX_CHARS = 300  # QQP questions longer than this are left out


def msmarco_archive() -> bytes:
    path = DATA / "queries.tar.gz"
    if not path.exists():
        DATA.mkdir(exist_ok=True)
        with urlopen(MSMARCO_URL, timeout=300) as response:
            path.write_bytes(response.read())
    return path.read_bytes()


def msmarco_queries(archive: bytes) -> dict[str, str]:
    """The split's queries, by MS MARCO's query id."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith(MSMARCO_SPLIT))
        handle = tar.extractfile(member)
        assert handle is not None
        text = handle.read().decode("utf-8")
    queries = {}
    for line in text.splitlines():
        qid, _, query = line.partition("\t")
        if query.strip():
            queries[qid] = query.strip()
    return queries


def normal(text: str) -> str:
    return " ".join(text.lower().split())


def qqp_paraphrase_pairs(archive: bytes) -> list[tuple[str, str, str]]:
    """QQP train's duplicate pairs (id, question1, question2) that B5 may use:
    two different questions, neither in B4's sample, neither too long."""
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    dev = qqp_rows(archive)  # B4's split
    used = {normal(t) for i in sample["ids"] for t in (dev[i].a, dev[i].b)}
    pairs = []
    for pair_id, p in sorted(qqp_rows(archive, QQP_TRAIN).items(), key=lambda kv: int(kv[0])):
        a, b = p.a.strip(), p.b.strip()
        if not p.duplicate or normal(a) == normal(b):
            continue
        if normal(a) in used or normal(b) in used or max(len(a), len(b)) > MAX_CHARS:
            continue
        pairs.append((pair_id, a, b))
    return pairs


def zipf_draws(rng: random.Random, n: int) -> list[int]:
    """n ranks (0-based) drawn with P(rank r) proportional to (r + 1) ** -s."""
    weights = [(r + 1) ** -ZIPF_S for r in range(CANDIDATES)]
    return rng.choices(range(CANDIDATES), weights=weights, k=n)


def repeats(draws: list[int]) -> int:
    return len(draws) - len(set(draws))


def draw(msmarco: dict[str, str], qqp: list[tuple[str, str, str]]) -> dict[str, Any]:
    """The stream, as sources and ids."""
    rng = random.Random(SEED)
    candidates = rng.sample(sorted(msmarco, key=int), CANDIDATES)  # rank order: random
    adversarial = [p for p in load_adversarial() if not p.duplicate]
    n_adversarial = round(ADVERSARIAL_SHARE * REQUESTS_TOTAL)

    # Paraphrases are PARAPHRASE_SHARE of all repeats: x / (base repeats + x).
    # Each takes two requests (its original and itself) from the base stream,
    # whose repeats depend on its length: settle x by iterating.
    paraphrases = 0
    for _ in range(50):
        base_n = REQUESTS_TOTAL - n_adversarial - 2 * paraphrases
        base = zipf_draws(random.Random(SEED + 1), base_n)
        wanted = round(PARAPHRASE_SHARE / (1 - PARAPHRASE_SHARE) * repeats(base))
        if wanted == paraphrases:
            break
        paraphrases = wanted
    else:
        raise RuntimeError("the paraphrase count didn't settle")
    chosen_qqp = rng.sample(qqp, paraphrases)

    gap = MIN_GAP / REQUESTS_TOTAL
    keyed: list[tuple[float, dict[str, Any]]] = []
    seen: set[int] = set()
    for j, rank in enumerate(base):
        kind = "repeat" if rank in seen else "first"
        seen.add(rank)
        item = {"kind": kind, "source": "msmarco", "id": candidates[rank], "rank": rank + 1}
        keyed.append(((j + 0.5) / base_n, item))

    def pair(first: dict[str, Any], second: dict[str, Any]) -> float:
        u1 = rng.uniform(0, 1 - 2 * gap)
        u2 = rng.uniform(u1 + gap, 1)
        keyed.append((u1, first))
        keyed.append((u2, second))
        return u1

    for pair_id, _, _ in chosen_qqp:
        pair(
            {"kind": "qqp-original", "source": "qqp", "id": pair_id, "side": "a"},
            {"kind": "qqp-paraphrase", "source": "qqp", "id": pair_id, "side": "b"},
        )
    first_asked: list[tuple[float, dict[str, Any]]] = []
    for p in adversarial:
        sides = ["a", "b"]
        rng.shuffle(sides)
        u = pair(
            {"kind": "near-miss-first", "source": "adversarial", "id": p.id, "side": sides[0]},
            {"kind": "near-miss", "source": "adversarial", "id": p.id, "side": sides[1]},
        )
        first_asked.append((u, {"id": p.id, "side": sides[0]}))
    for _ in range(n_adversarial - 2 * len(adversarial)):
        u, question = rng.choice(first_asked)  # repeats a pair's first question
        keyed.append(
            (
                rng.uniform(u + gap, 1),
                {"kind": "near-miss-repeat", "source": "adversarial", **question},
            )
        )

    keyed.sort(key=lambda kv: kv[0])
    stream = [item for _, item in keyed]
    assert len(stream) == REQUESTS_TOTAL, len(stream)
    return {
        "stream": stream,
        "base": {"draws": base_n, "distinct": len(set(base)), "repeats": repeats(base)},
        "paraphrases": paraphrases,
    }


def gaps(stream: list[dict[str, Any]], later: str) -> list[int]:
    """For each `later` request, how many requests after its pair's first."""
    first: dict[str, int] = {}
    out = []
    for i, item in enumerate(stream):
        key = f"{item['source']}:{item['id']}"
        if item["kind"] in ("qqp-original", "near-miss-first"):
            first[key] = i
        elif item["kind"] == later:
            out.append(i - first[key])
    return out


def build() -> None:
    msmarco_raw = msmarco_archive()
    qqp_raw = qqp_archive()
    msmarco = msmarco_queries(msmarco_raw)
    qqp = qqp_paraphrase_pairs(qqp_raw)
    drawn = draw(msmarco, qqp)
    stream = drawn["stream"]
    kinds = Counter(item["kind"] for item in stream)
    base = drawn["base"]
    top = Counter(item["rank"] for item in stream if item["source"] == "msmarco")
    workload = {
        "description": __doc__.split("\n\n")[0],
        "sources": {
            "msmarco": {
                "url": MSMARCO_URL,
                "split": MSMARCO_SPLIT,
                "sha256": hashlib.sha256(msmarco_raw).hexdigest(),
            },
            "qqp": {
                "url": QQP_URL,
                "split": QQP_TRAIN,
                "sha256": hashlib.sha256(qqp_raw).hexdigest(),
            },
            "adversarial": "bench/cache_eval/adversarial_pairs.jsonl (near-misses only)",
        },
        "parameters": {
            "seed": SEED,
            "requests": REQUESTS_TOTAL,
            "zipf_s": ZIPF_S,
            "candidates": CANDIDATES,
            "paraphrase_share_of_repeats": PARAPHRASE_SHARE,
            "adversarial_share": ADVERSARIAL_SHARE,
            "min_gap": MIN_GAP,
            "qqp_max_chars": MAX_CHARS,
        },
        "stats": {
            "kinds": dict(sorted(kinds.items())),
            "base_draws": base["draws"],
            "base_distinct": base["distinct"],
            "base_repeats": base["repeats"],
            "paraphrases": drawn["paraphrases"],
            "paraphrase_share_of_repeats": drawn["paraphrases"]
            / (base["repeats"] + drawn["paraphrases"]),
            "qqp_pairs_eligible": len(qqp),
            "most_asked": [{"rank": r, "requests": n} for r, n in top.most_common(5)],
            "paraphrase_gap_min": min(gaps(stream, "qqp-paraphrase")),
            "near_miss_gap_min": min(gaps(stream, "near-miss")),
        },
        "stream": stream,
    }
    WORKLOAD.write_text(
        json.dumps(workload, indent=None, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(json.dumps(workload["stats"], indent=1))
    print(f"wrote {WORKLOAD.relative_to(HERE.parent.parent)}")


def texts() -> list[dict[str, Any]]:
    """The committed stream with each request's text, from the checked downloads."""
    workload = json.loads(WORKLOAD.read_text(encoding="utf-8"))
    raw = {"msmarco": msmarco_archive(), "qqp": qqp_archive()}
    for source, data in raw.items():
        digest = hashlib.sha256(data).hexdigest()
        if digest != workload["sources"][source]["sha256"]:
            raise RuntimeError(
                f"{source}'s download has changed (sha256 {digest}): "
                "the stream can't be rebuilt from it"
            )
    msmarco = msmarco_queries(raw["msmarco"])
    qqp = qqp_rows(raw["qqp"], QQP_TRAIN)
    adversarial = {p.id: p for p in load_adversarial()}
    out = []
    for i, item in enumerate(workload["stream"]):
        if item["source"] == "msmarco":
            text = msmarco[item["id"]]
        else:
            p = qqp[item["id"]] if item["source"] == "qqp" else adversarial[item["id"]]
            text = (p.a if item["side"] == "a" else p.b).strip()
        out.append({"i": i, "kind": item["kind"], "text": text})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--text", action="store_true", help="write the stream's text locally")
    args = parser.parse_args()
    if args.text:
        requests = texts()
        DATA.mkdir(exist_ok=True)
        REQUESTS.write_text(json.dumps(requests), encoding="utf-8")
        print(f"wrote {len(requests)} requests to {REQUESTS}")
    else:
        build()


if __name__ == "__main__":
    main()
