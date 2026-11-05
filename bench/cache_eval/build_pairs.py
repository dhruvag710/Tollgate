"""Draw B4's QQP sample (SPEC §9): 5,000 pairs, half duplicates and half not,
from GLUE's labelled QQP split, with a fixed seed. Writes qqp_sample.json: the
pairs' QQP ids and the source archive's checksum, never the text.

    python bench/cache_eval/build_pairs.py

Run once; the committed sample is what every evaluation uses (pairs.load_qqp).
"""

import hashlib
import json
import random

from pairs import QQP_SPLIT, QQP_URL, SAMPLE, qqp_archive, qqp_rows

PAIRS_PER_LABEL = 2_500
SEED = 20261104


def main() -> None:
    archive = qqp_archive()
    rows = qqp_rows(archive)
    rng = random.Random(SEED)
    picked = []
    for duplicate in (True, False):
        ids = sorted((p.id for p in rows.values() if p.duplicate is duplicate), key=int)
        picked += rng.sample(ids, PAIRS_PER_LABEL)
    sample = {
        "source": QQP_URL,
        "split": QQP_SPLIT,
        "sha256": hashlib.sha256(archive).hexdigest(),
        "seed": SEED,
        "per_label": PAIRS_PER_LABEL,
        "ids": sorted(picked, key=int),
    }
    SAMPLE.write_text(json.dumps(sample, indent=1) + "\n", encoding="utf-8")
    print(f"{len(picked)} pairs drawn from {len(rows)} in {QQP_SPLIT}; ids in {SAMPLE.name}")


if __name__ == "__main__":
    main()
