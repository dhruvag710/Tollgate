"""B4's evaluation pairs (SPEC §9): Quora Question Pairs and the hand-written
adversarial set.

QQP's text isn't committed (Quora's terms don't clearly allow redistributing
it). It is downloaded from GLUE's distribution into bench/cache_eval/.data/
(not committed), its checksum checked, and the sampled pairs picked by the IDs
in qqp_sample.json, which build_pairs.py wrote: the same 5,000 pairs on every
machine.
"""

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
DATA = HERE / ".data"
QQP_URL = "https://dl.fbaipublicfiles.com/glue/data/QQP-clean.zip"
QQP_SPLIT = "QQP/dev.tsv"  # labelled; GLUE's test split isn't
SAMPLE = HERE / "qqp_sample.json"
ADVERSARIAL = HERE / "adversarial_pairs.jsonl"


@dataclass(frozen=True)
class Pair:
    id: str
    a: str
    b: str
    duplicate: bool  # True: should hit (the same question); False: must not
    category: str  # QQP's pairs: "qqp"; adversarial ones: what differs


def qqp_archive() -> bytes:
    """GLUE's QQP zip, downloaded once into .data/."""
    path = DATA / "QQP-clean.zip"
    if not path.exists():
        DATA.mkdir(exist_ok=True)
        with urlopen(QQP_URL, timeout=120) as response:
            path.write_bytes(response.read())
    return path.read_bytes()


def qqp_rows(archive: bytes, split: str = QQP_SPLIT) -> dict[str, Pair]:
    """Every labelled pair of the split (B4's: dev), by its QQP id."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        text = z.read(split).decode("utf-8")
    rows = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if not row.get("question1") or not row.get("question2"):
            continue
        rows[row["id"]] = Pair(
            row["id"], row["question1"], row["question2"], row["is_duplicate"] == "1", "qqp"
        )
    return rows


def load_qqp() -> list[Pair]:
    """The committed sample: downloaded, checked, picked by id."""
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    archive = qqp_archive()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != sample["sha256"]:
        raise RuntimeError(
            f"{QQP_URL} has changed (sha256 {digest}, the sample was drawn from "
            f"{sample['sha256']}): the sample can't be reproduced from it"
        )
    rows = qqp_rows(archive)
    return [rows[i] for i in sample["ids"]]


def load_adversarial(path: Path = ADVERSARIAL) -> list[Pair]:
    """The hand-written set. Each pair keeps its id (adv-<n>, the number it is
    reviewed and reported by): a pair dropped leaves a gap rather than
    renumbering the rest."""
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        pairs.append(
            Pair(item["id"], item["a"], item["b"], bool(item["duplicate"]), item["category"])
        )
    ids = [p.id for p in pairs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path.name}: an id is used twice")
    return pairs
