"""Tables for BENCHMARKS.md from one bench/run_v2.sh results directory (B5, B3).

Reads only what the harness recorded: k6's per-request records and summaries,
the gateway's ledger rows and metrics, the containers' cgroup counters, and the
host watcher's samples. Every number printed is computed from them here.

    python bench/v2_report.py bench/results/v2-<timestamp>

B5's records name, for every answer served, the question it was first given
for: the mock's answer is a function of its seed and the question
(mock_provider.app.generate_words), so each answer's hash is computed here for
every question of the workload (its text rebuilt locally by
bench/workloads/make_workload.py --text), and a semantic hit is checked
against the question it served.
"""

import gzip
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench" / "cache_eval"))

from scoring import percentile  # noqa: E402

WORKLOAD = ROOT / "bench" / "workloads" / "b5_workload.json"
REQUESTS = ROOT / "bench" / "workloads" / ".data" / "b5_requests.json"
MODEL = "gpt-4o-mini"
MOCK_SEED = 20261105  # bench/k6/cache_workload.js
MAX_TOKENS = 256
CLOCK_FLOOR = 0.85  # as machine.Watch
COMMIT_HEADROOM_MB = 1024  # less than this left to commit flags a run
# B3's steady state: every 1 s bucket of a 10 s window without a failed
# request, and with a p99 within twice the pre-kill p99 (or 10 ms of it).
STEADY_WINDOW_S = 10
BASELINE_S = 30


# --- small helpers ---------------------------------------------------------------------


def pct(values: Sequence[float], p: float) -> float | None:
    return percentile(values, p) if values else None


def ms(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def share(part: int, whole: int, decimals: int = 1) -> str:
    if not whole:
        return "n/a (0/0)"
    return f"{100 * part / whole:.{decimals}f}% ({part:,}/{whole:,})"


def p50_p99(values: Sequence[float], digits: int = 1) -> str:
    return f"{ms(pct(values, 50), digits)} / {ms(pct(values, 99), digits)}"


def records_text(path: Path) -> str | None:
    """A results file, or its gzipped form (the per-request records are
    committed gzipped); None if it is neither."""
    packed = path.with_name(path.name + ".gz")
    if packed.exists():
        return gzip.decompress(packed.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8") if path.exists() else None


def jsonl(path: Path) -> Iterable[dict[str, Any]]:
    text = records_text(path)
    if text is None:
        return
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:  # a line cut off as its writer stopped
                print(f"{path.name}: skipped an unreadable line", file=sys.stderr)


def failures_logged(path: Path) -> list[str]:
    text = records_text(path) or ""
    return [line for line in text.splitlines() if line.startswith("failure")]


def counters(path: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        name, *fields = line.split()
        out[name.lstrip("/")] = {k: int(v) for k, v in zip(fields[::2], fields[1::2], strict=True)}
    return out


def service_counters(before: Path, after: Path, service: str) -> dict[str, float] | None:
    b, a = counters(before), counters(after)
    names = [n for n in a if n in b and f"-{service}-" in n]
    if not names:
        return None

    def delta(key: str) -> int:
        return sum(a[n][key] - b[n][key] for n in names)

    return {
        "cpu_s": delta("usage_usec") / 1e6,
        "periods": delta("nr_periods"),
        "throttled": delta("nr_throttled"),
        "throttled_s": delta("throttled_usec") / 1e6,
        "memory_peak": max(a[n].get("memory_peak", 0) for n in names),
        "memory_current": max(a[n].get("memory_current", 0) for n in names),
        # At the end: memory the processes hold, and file cache charged to them.
        "memory_anon": max(a[n].get("memory_anon", 0) for n in names),
        "memory_file": max(a[n].get("memory_file", 0) for n in names),
    }


def host(path: Path) -> dict[str, Any]:
    """The host watcher's samples: how busy, its clocks, battery; and flags."""
    samples = list(jsonl(path))
    if not samples:
        return {"samples": 0, "flags": ["host not watched"]}
    busy = [s["busy"] for s in samples]
    limits = [s["mhz_limit"] for s in samples if s["mhz_limit"] is not None]
    fastest = [s["mhz_fastest"] for s in samples if s["mhz_fastest"] is not None]
    maximum = max((s["mhz_max"] for s in samples if s["mhz_max"]), default=None)
    flags = []
    # A limit or a fastest core under 100 MHz is a reading no running CPU
    # gives (B4 met it too, BENCHMARKS.md): reported apart from throttling.
    impossible = sum(
        1 for s in samples
        if (s["mhz_limit"] is not None and s["mhz_limit"] < 100)
        or (s["mhz_fastest"] is not None and s["mhz_fastest"] < 100)
    )  # fmt: skip
    if impossible:
        flags.append(f"impossible clock reading (under 100 MHz) in {impossible} of "
                     f"{len(samples)} samples")  # fmt: skip
    limits = [x for x in limits if x >= 100]
    fastest = [x for x in fastest if x >= 100]
    low_limit = [x for x in limits if maximum and x < CLOCK_FLOOR * maximum]
    if low_limit:
        flags.append(
            f"clock limit under {CLOCK_FLOOR:.0%} of max in {len(low_limit)} of "
            f"{len(limits)} samples (lowest {min(low_limit):.0f} MHz)"
        )
    if any(s["on_battery"] for s in samples):
        flags.append("on battery")
    headroom = [
        s["commit_limit_mb"] - s["commit_mb"] for s in samples if s.get("commit_mb") is not None
    ]
    if headroom and min(headroom) < COMMIT_HEADROOM_MB:
        flags.append(f"within {COMMIT_HEADROOM_MB:,} MB of the commit limit "
                     f"(least headroom {min(headroom):,.0f} MB)")  # fmt: skip
    return {
        "samples": len(samples),
        "busy_mean": statistics.fmean(busy),
        "busy_max": max(busy),
        "fastest_min": min(fastest) if fastest else None,
        "fastest_median": statistics.median(fastest) if fastest else None,
        "mhz_max": maximum,
        "commit_headroom_min_mb": min(headroom) if headroom else None,
        "flags": flags,
    }


def host_text(h: dict[str, Any]) -> str:
    if not h["samples"]:
        return "not watched"
    flags = "; ".join(h["flags"]) or "none"
    return (
        f"host {h['busy_mean']:.0f}% busy on average (max {h['busy_max']:.0f}%), fastest "
        f"core median {h['fastest_median']:,.0f} MHz of {h['mhz_max']:,.0f}; flags: {flags}"
    )


def throttling(c: dict[str, float] | None) -> str:
    if c is None:
        return "not recorded"
    return (
        f"{c['cpu_s']:.0f} CPU s, throttled in {c['throttled']:,} of {c['periods']:,} "
        f"periods ({c['throttled_s']:.1f} s)"
    )


# --- B5 ----------------------------------------------------------------------------------


def answer_hash(text: str, n_tokens: int) -> str:
    """The first 16 hex digits of the SHA-256 of the mock's answer to `text`,
    as bench/k6/cache_workload.js hashes the answers it is served."""
    from mock_provider.app import generate_words

    messages = [{"role": "user", "content": text}]
    seed = json.dumps([MOCK_SEED, MODEL, messages], sort_keys=True).encode()
    return hashlib.sha256(" ".join(generate_words(seed, n_tokens)).encode()).hexdigest()[:16]


def workload() -> tuple[list[dict[str, Any]], list[str]]:
    stream = json.loads(WORKLOAD.read_text(encoding="utf-8"))["stream"]
    texts = [r["text"] for r in json.loads(REQUESTS.read_text(encoding="utf-8"))]
    return stream, texts


def partner_text(stream: list[dict[str, Any]], texts: list[str]) -> dict[int, int]:
    """For each paraphrase and near-miss, the index of its pair's first question."""
    first: dict[str, int] = {}
    out = {}
    for i, item in enumerate(stream):
        key = f"{item['source']}:{item['id']}"
        if item["kind"] in ("qqp-original", "near-miss-first"):
            first[key] = i
        elif item["kind"] in ("qqp-paraphrase", "near-miss"):
            out[i] = first[key]
    return out


SEMANTIC_LABELS = {
    "qqp-paraphrase": "QQP paraphrase",
    "near-miss": "adversarial near-miss",
    "first": "MS MARCO first ask",
    "qqp-original": "QQP original",
    "near-miss-first": "adversarial first ask",
    "repeat": "MS MARCO repeat",
    "near-miss-repeat": "adversarial repeat",
}


def b5_config(
    root: Path,
    config: str,
    stream: list[dict[str, Any]],
    texts: list[str],
    by_hash: dict[str, set[int]],
    partner: dict[int, int],
) -> dict[str, Any] | None:
    base = root / "b5" / config
    if not base.with_suffix(".summary.json").exists():
        return None
    summary = json.loads(base.with_suffix(".summary.json").read_text(encoding="utf-8"))
    records = list(jsonl(base.with_suffix(".records.jsonl")))
    replay = sorted((r for r in records if "i" in r), key=lambda r: r["i"])
    probes = [r for r in records if "p" in r]
    out: dict[str, Any] = {"config": config, "summary": summary, "requests": len(replay)}

    def text_of(i: int) -> str:
        return texts[i]

    kinds = Counter(stream[r["i"]]["kind"] for r in replay)
    ok = [r for r in replay if r["s"] == 200]
    hits = [r for r in ok if r["c"] == "HIT"]
    semantic_hits = [r for r in hits if r.get("o") == "hit"]
    exact_hits = [r for r in hits if r.get("o") != "hit"]
    misses = [r for r in ok if r["c"] == "MISS"]
    out.update(
        kinds=kinds,
        ok=len(ok),
        failed=len(replay) - len(ok),
        hits=len(hits),
        exact_hits=len(exact_hits),
        semantic_hits=len(semantic_hits),
        misses=len(misses),
    )

    # What each semantic hit served.
    verdicts: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for r in semantic_hits:
        i, kind = r["i"], stream[r["i"]]["kind"]
        served = by_hash.get(r["h"], set())
        if i in partner and any(texts[j] == texts[partner[i]] for j in served):
            verdict = (
                "its pair's first question" if kind == "qqp-paraphrase" else "false: its near-miss"
            )
        elif any(texts[j] == text_of(i) for j in served):
            verdict = "the same question"
        elif not served:
            verdict = "unknown answer"
        else:
            verdict = "another question"
        verdicts[verdict] += 1
        by_kind[kind][verdict] += 1
    out["semantic_verdicts"] = verdicts
    out["semantic_by_kind"] = by_kind
    # Exact hits must serve the request's own question.
    out["exact_wrong"] = sum(
        1
        for r in exact_hits
        if not any(texts[j] == text_of(r["i"]) for j in by_hash.get(r["h"], set()))
    )

    # Outcomes of the semantic layer, from the Server-Timing notes.
    outcomes = Counter(r.get("o", "none") for r in ok if r["c"] in ("HIT", "MISS"))
    out["outcomes"] = outcomes
    looked_up = [r for r in ok if "l" in r]  # the lookup ran
    miss_path = [r.get("ew", 0) + r.get("e", 0) + r["l"] for r in looked_up]
    out["miss_path"] = miss_path
    out["embed"] = [r["e"] for r in looked_up if "e" in r]
    out["embed_wait"] = [r.get("ew", 0) for r in looked_up]
    out["lookup"] = [r["l"] for r in looked_up]
    checked = [r for r in ok if "v" in r or "vw" in r]
    out["checks"] = len(checked)

    def check_ms(r: dict[str, Any]) -> float:
        return float(r.get("vw", 0) + r.get("v", 0))

    def path_ms(r: dict[str, Any]) -> float:
        return float(r.get("ew", 0) + r.get("e", 0) + r.get("l", 0) + check_ms(r))

    out["hit_path"] = [path_ms(r) for r in semantic_hits]
    out["hit_check"] = [check_ms(r) for r in semantic_hits]
    rejected = [r for r in ok if r.get("o") == "rejected"]
    out["rejected"] = len(rejected)
    out["rejected_check"] = [check_ms(r) for r in rejected]
    out["rejected_path"] = [path_ms(r) for r in rejected]
    out["rejected_by_kind"] = Counter(stream[r["i"]]["kind"] for r in rejected)
    out["looked_up"] = len(looked_up)
    out["lookups"] = sum(n for o, n in outcomes.items() if o != "none")

    # Latency as the client saw it.
    out["latency"] = {
        "exact hit": [r["t"] for r in exact_hits],
        "semantic hit": [r["t"] for r in semantic_hits],
        "miss": [r["t"] for r in misses],
        "all": [r["t"] for r in ok],
    }
    out["gateway_hit"] = [r["g"] for r in semantic_hits if "g" in r]

    # The lookup as the cache grows: by quarter of the run.
    quarters = []
    stored = 0
    n = len(replay)
    s_rate = summary["rate"]
    for q in range(4):
        part = [r for r in replay[q * n // 4 : (q + 1) * n // 4] if r["s"] == 200]
        stored += sum(1 for r in part if r["c"] == "MISS" and r.get("o") in ("miss", "rejected"))
        lookups = [r["l"] for r in part if "l" in r]
        paths = [r.get("ew", 0) + r.get("e", 0) + r["l"] for r in part if "l" in r]
        quarters.append(
            {
                "quarter": q + 1,
                "stored_by_end": stored,
                "lookup": lookups,
                "miss_path": paths,
                "embed": [r["e"] for r in part if "e" in r],
                # Checks run in this quarter, a second: the CPU they take from the rest.
                "checks_per_s": sum(1 for r in part if "v" in r) / (len(part) / s_rate)
                if part
                else 0.0,
            }
        )
    out["quarters"] = quarters
    entries = base.with_suffix(".cache-entries.txt")
    out["entries"] = (
        int(entries.read_text(encoding="utf-8").strip() or 0) if entries.exists() else None
    )

    # Probes: overhead for non-cacheable requests.
    for phase in ("idle", "replay"):
        g = [
            r["t"]
            for r in probes
            if r["p"] == "g" and r.get("phase", "replay") == phase and r["s"] == 200
        ]
        m = [
            r["t"]
            for r in probes
            if r["p"] == "m" and r.get("phase", "replay") == phase and r["s"] == 200
        ]
        failed = sum(1 for r in probes if r.get("phase", "replay") == phase and r["s"] != 200)
        out[f"probe_{phase}"] = {"gateway": g, "mock": m, "failed": failed}

    # Cost, from the gateway's ledger.
    ledger = []
    path = base.with_suffix(".ledger.csv")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            cache, status, count, prompt, completion, cost, saved = line.split(",")
            ledger.append(
                {
                    "cache": cache,
                    "status": status,
                    "count": int(count),
                    "prompt": int(prompt or 0),
                    "completion": int(completion or 0),
                    "cost": float(cost or 0),
                    "saved": float(saved or 0),
                }
            )
    out["ledger"] = ledger
    out["cost"] = sum(x["cost"] for x in ledger)
    out["saved"] = sum(x["saved"] for x in ledger)
    out["gateway"] = service_counters(
        base.with_suffix(".counters-before.txt"), base.with_suffix(".counters-after.txt"), "gateway"
    )
    out["postgres"] = service_counters(
        base.with_suffix(".counters-before.txt"),
        base.with_suffix(".counters-after.txt"),
        "postgres",
    )
    out["host"] = host(base.with_suffix(".host.jsonl"))
    out["failures_logged"] = failures_logged(base.with_suffix(".log")) + failures_logged(
        base.with_suffix(".records.jsonl")
    )
    metrics = base.with_suffix(".metrics.txt")
    out["metrics"] = {
        m.group(1): float(m.group(2))
        for m in re.finditer(
            r'^tollgate_cache_lookups_total\{layer="semantic",result="(\w+)"\} (\S+)$',
            metrics.read_text(encoding="utf-8") if metrics.exists() else "",
            re.M,
        )
    }
    return out


def b5(root: Path) -> list[str]:
    if not (root / "b5").exists():
        return []
    stream, texts = workload()
    partner = partner_text(stream, texts)
    configs = {}
    by_hash: dict[str, set[int]] = {}
    for config in ("off", "on"):
        summary_path = root / "b5" / f"{config}.summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        n_tokens = min(summary["output_tokens"], MAX_TOKENS)
        if not by_hash:
            hashes: dict[str, str] = {}
            for i, text in enumerate(texts):
                if text not in hashes:
                    hashes[text] = answer_hash(text, n_tokens)
                by_hash.setdefault(hashes[text], set()).add(i)
        result = b5_config(root, config, stream, texts, by_hash, partner)
        if result:
            configs[config] = result
    if not configs:
        return []
    lines = ["### B5: cache impact", ""]
    parity = root / "b5" / "model-parity.json"
    if parity.exists():
        p = json.loads(parity.read_text(encoding="utf-8"))
        lines += [
            f"Models in the gateway image against B4's numbers, {p['pairs']} pairs: largest "
            f"similarity difference {p['max_similarity_difference']:.1e}, largest check-score "
            f"difference {p['max_score_difference']:.1e}; decisions that differ: "
            f"{len(p['decisions_that_differ'])}.",
            "",
        ]
    first = next(iter(configs.values()))
    s = first["summary"]
    lines += [
        f"{first['requests']:,} requests at {s['rate']}/s; the mock answers each in "
        f"{s['mock_latency_ms']} ms, {s['output_tokens']} tokens. Probes: {s['probe_rate']}/s "
        "each way.",
        "",
        "| | Layer off | Layer on |",
        "|---|---|---|",
    ]

    def row(label: str, get: Callable[[dict[str, Any]], str]) -> None:
        cells = [get(configs[c]) if c in configs else "not run" for c in ("off", "on")]
        lines.append(f"| {label} | {' | '.join(cells)} |")

    row("Hit rate, all", lambda c: share(c["hits"], c["ok"]))
    row("Exact-layer hits", lambda c: share(c["exact_hits"], c["ok"]))
    row("Semantic-layer hits", lambda c: share(c["semantic_hits"], c["ok"]))
    row(
        "Cost saved (share of what the replay would cost uncached)",
        lambda c: (
            f"{100 * c['saved'] / (c['cost'] + c['saved']):.1f}% (${c['saved']:.4f} of "
            f"${c['cost'] + c['saved']:.4f})"
            if c["cost"] + c["saved"]
            else "n/a"
        ),
    )
    row("Latency p50 / p99, exact hits (ms)", lambda c: p50_p99(c["latency"]["exact hit"]))
    row("Latency p50 / p99, semantic hits (ms)", lambda c: p50_p99(c["latency"]["semantic hit"]))
    row("Latency p50 / p99, misses (ms)", lambda c: p50_p99(c["latency"]["miss"]))
    row(
        "Miss path p50 / p95 / p99 (ms): embed, its wait, lookup",
        lambda c: (
            (
                f"{ms(pct(c['miss_path'], 50))} / {ms(pct(c['miss_path'], 95))} / "
                f"{ms(pct(c['miss_path'], 99))}"
            )
            if c["miss_path"]
            else "n/a"
        ),
    )
    row("Embed p50 / p99 (ms)", lambda c: p50_p99(c["embed"]) if c["embed"] else "n/a")
    row(
        "Wait for the embedding thread p50 / p99 (ms)",
        lambda c: p50_p99(c["embed_wait"]) if c["embed_wait"] else "n/a",
    )
    row("Lookup p50 / p99 (ms)", lambda c: p50_p99(c["lookup"]) if c["lookup"] else "n/a")
    row(
        "Hit path p50 / p95 / p99 (ms): miss path, then the check",
        lambda c: (
            (
                f"{ms(pct(c['hit_path'], 50))} / {ms(pct(c['hit_path'], 95))} / "
                f"{ms(pct(c['hit_path'], 99))}"
            )
            if c["hit_path"]
            else "n/a"
        ),
    )
    row(
        "Rejected candidates (share of semantic lookups)",
        lambda c: share(c["rejected"], c["lookups"]) if c["lookups"] else "n/a",
    )
    row(
        "Their check, its wait included, p50 / p99 (ms)",
        lambda c: p50_p99(c["rejected_check"]) if c["rejected_check"] else "n/a",
    )
    row(
        "Their whole semantic lookup p50 / p99 (ms)",
        lambda c: p50_p99(c["rejected_path"]) if c["rejected_path"] else "n/a",
    )
    row(
        "Skipped: pool full / timed out / failed",
        lambda c: " / ".join(
            str(sum(n for o, n in c["outcomes"].items() if o.endswith(w)))
            for w in ("full", "timeout", "error")
        ),
    )

    def overhead(c: dict[str, Any], phase: str, p: float) -> float | None:
        g, m = c[f"probe_{phase}"]["gateway"], c[f"probe_{phase}"]["mock"]
        return None if not g or not m else percentile(g, p) - percentile(m, p)

    for phase, label in (("idle", "probes alone"), ("replay", "during the replay")):
        row(
            f"Non-cacheable overhead p50 / p95 / p99 (ms), {label}",
            lambda c, ph=phase: " / ".join(ms(overhead(c, ph, p), 2) for p in (50, 95, 99)),
        )
        row(
            f"Probe requests (gateway / mock), {label}",
            lambda c, ph=phase: (
                f"{len(c[f'probe_{ph}']['gateway']):,} / "
                f"{len(c[f'probe_{ph}']['mock']):,}, failed "
                f"{c[f'probe_{ph}']['failed']}"
            ),
        )
    row("Replay requests failed", lambda c: share(c["failed"], c["requests"], 2))
    row("Not started on time (k6)", lambda c: str(int(c["summary"]["replay"]["dropped"])))
    row(
        "Cache entries at the end",
        lambda c: f"{c['entries']:,}" if c["entries"] is not None else "n/a",
    )
    row("Gateway CPU (cgroup)", lambda c: throttling(c["gateway"]))
    row(
        "Gateway memory at the end: held by processes / file cache (MiB)",
        lambda c: (
            f"{c['gateway']['memory_anon'] / 2**20:,.0f} / "
            f"{c['gateway']['memory_file'] / 2**20:,.0f}"
            if c["gateway"]
            else "n/a"
        ),
    )
    row(
        "Gateway memory peak / at the end (MiB), file cache included",
        lambda c: (
            f"{c['gateway']['memory_peak'] / 2**20:,.0f} / "
            f"{c['gateway']['memory_current'] / 2**20:,.0f}"
            if c["gateway"]
            else "n/a"
        ),
    )
    row("Host", lambda c: host_text(c["host"]))
    lines.append("")

    on = configs.get("on")
    if on:
        lines += [
            "Semantic hits by what they served (layer on):",
            "",
            "| Request | Semantic hits | Served |",
            "|---|---|---|",
        ]
        for kind, verdicts in sorted(on["semantic_by_kind"].items()):
            served = "; ".join(f"{v}: {n:,}" for v, n in verdicts.most_common())
            lines.append(
                f"| {SEMANTIC_LABELS.get(kind, kind)} | {sum(verdicts.values()):,} | {served} |"
            )
        paraphrases = on["kinds"].get("qqp-paraphrase", 0)
        near_misses = on["kinds"].get("near-miss", 0)
        true_hits = on["semantic_by_kind"].get("qqp-paraphrase", Counter())[
            "its pair's first question"
        ]
        false_hits = on["semantic_by_kind"].get("near-miss", Counter())["false: its near-miss"]
        lines += [
            "",
            f"- QQP paraphrases answered with their pair's answer: "
            f"{share(true_hits, paraphrases)}.",
            f"- Adversarial near-misses answered with their partner's answer (false hits): "
            f"{share(false_hits, near_misses, 2)}.",
            f"- Exact-layer hits that served another question's answer: {on['exact_wrong']}.",
            "",
            "Outcomes of the semantic lookups (layer on): "
            + ", ".join(f"{o}: {n:,}" for o, n in on["outcomes"].most_common() if o != "none")
            + ".",
            "",
            "Rejected candidates by request: "
            + ", ".join(
                f"{SEMANTIC_LABELS.get(k, k)} {n:,}"
                for k, n in on["rejected_by_kind"].most_common()
            )
            + ".",
            "",
            "The lookup as the cache grows (layer on), by quarter of the replay:",
            "",
            "| Quarter | Entries stored by its end | Checks a second | Embed p50 / p99 (ms) "
            "| Lookup p50 / p99 (ms) | Miss path p50 / p99 (ms) |",
            "|---|---|---|---|---|---|",
        ]
        for q in on["quarters"]:
            lines.append(
                f"| {q['quarter']} | {q['stored_by_end']:,} | {q['checks_per_s']:.1f} | "
                f"{p50_p99(q['embed'])} | {p50_p99(q['lookup'])} | {p50_p99(q['miss_path'])} |"
            )
        if on["metrics"]:
            lines += [
                "",
                "The gateway's own counters (tollgate_cache_lookups_total, semantic): "
                + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(on["metrics"].items()))
                + ".",
            ]
        lines.append("")
    for c in configs.values():
        if c["failures_logged"]:
            lines += [
                f"Failures logged (layer {c['config']}): {len(c['failures_logged'])}; first: "
                f"`{c['failures_logged'][0][:200]}`",
                "",
            ]
    return lines


# --- B3 ----------------------------------------------------------------------------------


def epoch_ms(rfc3339: str) -> float:
    """Docker's FinishedAt (nanoseconds, Z) as ms since the epoch."""
    text = rfc3339.strip().replace("Z", "+00:00")
    main, _, rest = text.partition(".")
    fraction, _, zone = rest.partition("+")
    stamp = datetime.fromisoformat(f"{main}.{fraction[:6].ljust(6, '0')}+{zone}")
    return stamp.timestamp() * 1000


def b3_run(path: Path) -> dict[str, Any]:
    stem = path.name.removesuffix(".summary.json")
    base = path.with_name(stem)
    summary = json.loads(path.read_text(encoding="utf-8"))
    records = list(jsonl(base.with_name(stem + ".records.jsonl")))
    requests = [r for r in records if "s" in r and "breakers" not in r]
    polls = [r for r in records if "breakers" in r]
    killed = epoch_ms(base.with_name(stem + ".killed-at.txt").read_text(encoding="utf-8"))
    for r in requests:
        r["rel"] = (r["at"] - killed) / 1000
    failed = [r for r in requests if r["s"] != 200]
    before = [r["t"] for r in requests if -BASELINE_S <= r["rel"] < 0 and r["s"] == 200]
    base_p99 = percentile(before, 99) if before else 0.0
    limit = max(2 * base_p99, base_p99 + 10)
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in requests:
        buckets[math.floor(r["rel"])].append(r)
    last = max(buckets) if buckets else 0

    def calm(second: int) -> bool:
        rs = buckets.get(second, [])
        return (
            bool(rs)
            and all(r["s"] == 200 for r in rs)
            and percentile([r["t"] for r in rs], 99) <= limit
        )

    steady = None
    for second in range(0, last - STEADY_WINDOW_S + 2):
        if all(calm(second + k) for k in range(STEADY_WINDOW_S)):
            steady = second
            break
    # The failover window: from the kill to steady state, or to the end of the
    # run if it never came (at least the first second either way).
    window_end = max(steady, 1) if steady is not None else math.inf
    in_window = [r for r in requests if 0 <= r["rel"] < window_end]
    # Each replica's own count of failed attempts at each provider (replicas.txt).
    per_replica: dict[str, dict[str, int]] = {}
    rpath = base.with_name(stem + ".replicas.txt")
    if rpath.exists():
        replica = ""
        for line in rpath.read_text(encoding="utf-8").splitlines():
            if line.startswith("== "):
                replica = line[3:].strip().lstrip("/")
                per_replica[replica] = {}
            found = re.match(r'tollgate_provider_attempts_total\{provider="([^"]+)",'
                             r'result="([^"]+)"\} (\S+)', line)  # fmt: skip
            if found and replica:
                key = f"{found.group(1)} {found.group(2)}"
                per_replica[replica][key] = int(float(found.group(3)))
    opened = next(
        (
            p["at"]
            for p in polls
            if p["at"] >= killed
            and any(
                x.get("name") == "mock" and x.get("state") == "open"
                for x in (p["breakers"] or {}).get("providers", [])
            )
        ),
        None,
    )
    secondary_opened = next(
        (
            p["at"]
            for p in polls
            if p["at"] >= killed
            and any(
                x.get("name") == "mock-secondary" and x.get("state") == "open"
                for x in (p["breakers"] or {}).get("providers", [])
            )
        ),
        None,
    )
    after = [r for r in requests if r["rel"] >= 0]
    tens: dict[int, int] = defaultdict(int)
    for r in after:
        tens[math.floor(r["rel"] / 10)] += r["s"] != 200
    timeline = []
    for second in range(-3, min(last, 15) + 1):
        rs = buckets.get(second, [])
        if rs:
            timeline.append(
                {
                    "second": second,
                    "requests": len(rs),
                    "failed": sum(1 for r in rs if r["s"] != 200),
                    "p50": percentile([r["t"] for r in rs], 50),
                    "p99": percentile([r["t"] for r in rs], 99),
                    "max": max(r["t"] for r in rs),
                    "secondary": sum(1 for r in rs if r.get("p") == "mock-secondary"),
                }
            )
    ledger = []
    lpath = base.with_name(stem + ".ledger.csv")
    if lpath.exists():
        for line in lpath.read_text(encoding="utf-8").splitlines():
            status, provider, attempts, count = line.split(",")
            ledger.append((status, provider, int(attempts), int(count)))
    return {
        "name": stem,
        "rate": summary["rate"],
        "requests": len(requests),
        "failed": len(failed),
        "failed_after_kill": sum(1 for r in failed if r["rel"] >= 0),
        "errors": Counter(f"{r['s']} {r.get('e', '')}".strip() for r in failed),
        "dropped": summary["dropped"],
        "baseline_p99": base_p99,
        "steady_s": steady,
        "per_replica": per_replica,
        "secondary_open_s": None
        if secondary_opened is None
        else (secondary_opened - killed) / 1000,
        "failures_per_10s": [tens[k] for k in range(max(tens) + 1)] if tens else [],
        "window_requests": len(in_window),
        "window_failed": sum(1 for r in in_window if r["s"] != 200),
        "breaker_open_s": None if opened is None else (opened - killed) / 1000,
        "max_after_kill": max((r["t"] for r in after), default=None),
        "p99_after_kill_10s": pct([r["t"] for r in after if r["rel"] < 10], 99),
        "timeline": timeline,
        "retried": sum(c for status, _, attempts, c in ledger if attempts > 1),
        "ledger": ledger,
        "host": host(base.with_name(stem + ".host.jsonl")),
        "gateway": service_counters(
            base.with_name(stem + ".counters-before.txt"),
            base.with_name(stem + ".counters-after.txt"),
            "gateway",
        ),
    }


def b3(root: Path) -> list[str]:
    runs = [b3_run(p) for p in sorted((root / "b3").glob("rate-*-run-*.summary.json"))]
    if not runs:
        return []
    lines = [
        "### B3: failover",
        "",
        "Steady state: the first second after the kill from which every 1 s bucket of a "
        f"{STEADY_WINDOW_S} s window has no failed request and a p99 within twice the "
        f"{BASELINE_S} s pre-kill p99 (or 10 ms of it). Requests are bucketed by when they "
        "started. The failover window runs from the kill to steady state, or to the end of the "
        "run if it never came.",
        "",
        "| Run | Requests | Failed, whole run | Failed in the failover window | Time to steady "
        "state | Primary's breaker open after | Secondary's breaker opened | Pre-kill p99 (ms) | "
        "p99, first 10 s after (ms) | Retried on the secondary | Not started on time (k6) | "
        "Host |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        steady = "not reached" if r["steady_s"] is None else f"{r['steady_s']} s"
        opened = "n/a" if r["breaker_open_s"] is None else f"{r['breaker_open_s']:.1f} s"
        second = "no" if r["secondary_open_s"] is None else f"at {r['secondary_open_s']:.1f} s"
        flags = "; ".join(r["host"]["flags"]) or "no flags"
        lines.append(
            f"| {r['name']} | {r['requests']:,} | {share(r['failed'], r['requests'], 2)} "
            f"| {share(r['window_failed'], r['window_requests'], 2)} | {steady} | {opened} "
            f"| {second} | {r['baseline_p99']:.1f} | {ms(r['p99_after_kill_10s'])} "
            f"| {r['retried']:,} | {int(r['dropped']):,} | {flags} |"
        )
    lines += [
        "",
        "Each replica's attempts at the secondary (successes / timeouts), from its own "
        "counters, and the run's failures in each 10 s after the kill:",
        "",
        "| Run | Replica 1 | Replica 2 | Replica 3 | Failures per 10 s after the kill |",
        "|---|---|---|---|---|",
    ]
    for r in runs:
        cells = []
        for replica in sorted(r["per_replica"]):
            counts = r["per_replica"][replica]
            cells.append(f"{counts.get('mock-secondary success', 0):,} / "
                         f"{counts.get('mock-secondary timeout', 0):,}")  # fmt: skip
        cells += ["n/a"] * (3 - len(cells))
        series = ", ".join(str(n) for n in r["failures_per_10s"])
        lines.append(f"| {r['name']} | {' | '.join(cells[:3])} | {series} |")
    lines.append("")
    by_rate: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_rate[r["rate"]].append(r)
    for rate, rs in sorted(by_rate.items()):
        steady = [r["steady_s"] for r in rs if r["steady_s"] is not None]
        failed = sum(r["failed"] for r in rs)
        total = sum(r["requests"] for r in rs)
        lines.append(
            f"- {rate} RPS, {len(rs)} runs: failed {share(failed, total, 3)} in all; time "
            f"to steady state {', '.join(f'{x} s' for x in steady) or 'never'}"
            + (f" (median {statistics.median(steady):g} s)" if steady else "")
            + "."
        )
    for r in runs:
        if r["errors"]:
            lines.append(
                f"- {r['name']} failures: " + ", ".join(f"{k}: {n}" for k, n in r["errors"].items())
            )
    lines += ["", "Each run's first seconds after the kill (second 0 starts at the kill):", ""]
    for r in runs[:1] + [x for x in runs[1:] if x["failed_after_kill"] or (x["steady_s"] or 0) > 2]:
        lines += [
            f"{r['name']}:",
            "",
            "| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | "
            "Served by the secondary |",
            "|---|---|---|---|---|---|---|",
        ]
        for t in r["timeline"]:
            lines.append(
                f"| {t['second']} | {t['requests']} | {t['failed']} | {t['p50']:.1f} | "
                f"{t['p99']:.1f} | {t['max']:.0f} | {t['secondary']} |"
            )
        lines.append("")
    return lines


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    root = Path(sys.argv[1])
    out = [f"## Results: `{root.as_posix()}`", ""]
    env = root / "environment.txt"
    if env.exists():
        out += ["```", *env.read_text(encoding="utf-8").splitlines()[:40], "```", ""]
    out += b5(root)
    out += b3(root)
    print("\n".join(out))


if __name__ == "__main__":
    main()
