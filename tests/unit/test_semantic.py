"""The semantic layer's parts that need no Postgres (SPEC Phase 2.4): the
question a request asks it, the wrappers around the real models (driven here
by stand-ins for sentence-transformers, which CI doesn't install), how a
two-stage lookup ends on every path (with the Postgres lookup stood in for),
and writes in the background that fail."""

import asyncio
import logging
import math
import sys
import threading
import types
from typing import Any, cast

import pytest

from tests.helpers import StandInVerifier, WordEmbedder
from tollgate.cache.embedder import SentenceTransformerEmbedder
from tollgate.cache.policy import exact_key
from tollgate.cache.semantic import Found, Limits, Question, SemanticCache, question_of
from tollgate.cache.verifier import NLIVerifier
from tollgate.observability.metrics import Metrics
from tollgate.observability.timing import Timings

SYSTEM = {"role": "system", "content": "You answer briefly."}
ASK = {"role": "user", "content": "what is the capital of austria"}
BODY: dict[str, Any] = {"model": "gpt-4o-mini", "temperature": 0, "messages": [SYSTEM, ASK]}

# --- the question ------------------------------------------------------------------------


def test_the_question_is_the_last_user_message_and_its_scope_everything_else() -> None:
    assert question_of(BODY) == Question(
        exact_key({**BODY, "messages": [SYSTEM]}), "what is the capital of austria"
    )


def test_a_question_sent_as_text_parts_is_their_text() -> None:
    parts = [
        {"type": "text", "text": "what is the capital "},
        {"type": "text", "text": "of austria"},
    ]
    question = question_of({**BODY, "messages": [SYSTEM, {"role": "user", "content": parts}]})
    assert question == question_of(BODY)  # the same text, and the same scope


@pytest.mark.parametrize(
    "messages",
    [
        [],
        "what is the capital of austria",  # not a list of messages
        [SYSTEM, "what is the capital of austria"],  # the last one isn't a message
        [ASK, {"role": "assistant", "content": "Vienna."}],  # the last one isn't the user's
        [SYSTEM, {"role": "user"}],
        [SYSTEM, {"role": "user", "content": " \n "}],
        [SYSTEM, {"role": "user", "content": [{"type": "text", "text": ""}]}],
        [SYSTEM, {"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                              {"type": "image_url", "image_url": {"url": "x"}}]}],
    ],
)  # fmt: skip
def test_a_request_without_a_question_in_text_has_none(messages: Any) -> None:
    assert question_of({**BODY, "messages": messages}) is None
    assert question_of({"model": "gpt-4o-mini"}) is None  # no messages at all


# --- the real model's wrapper ------------------------------------------------------------


class StandInModel:
    """Stands in for sentence_transformers.SentenceTransformer."""

    def __init__(self, name: str, device: str, revision: str | None = None) -> None:
        self.name, self.device, self.revision = name, device, revision
        self.encoded: list[tuple[list[str], dict[str, Any]]] = []
        self.dtype = "as stored"

    def float(self) -> "StandInModel":
        self.dtype = "float32"
        return self

    def get_sentence_embedding_dimension(self) -> int:  # sentence-transformers < 5
        return 3

    def encode(self, texts: list[str], **options: Any) -> list[list[float]]:
        self.encoded.append((texts, options))
        return [[0.6, 0.8, 0.0] for _ in texts]


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand-ins for torch and sentence_transformers; what they were asked."""
    seen: dict[str, Any] = {}

    def model(name: str, device: str, revision: str | None = None) -> StandInModel:
        seen["model"] = StandInModel(name, device, revision)
        return seen["model"]

    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda n: seen.__setitem__("threads", n)  # type: ignore[attr-defined]
    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    return seen


def test_the_model_runs_on_the_cpu_on_one_thread_unless_told_otherwise(
    library: dict[str, Any],
) -> None:
    embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    assert library["threads"] == 1
    assert (library["model"].name, library["model"].device) == ("BAAI/bge-small-en-v1.5", "cpu")
    assert library["model"].dtype == "float32"  # gte-small is stored in float16
    assert embedder.dimensions == 3
    assert library["model"].revision is None  # the Hub's latest, unless pinned
    SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", threads=4)
    assert library["threads"] == 4
    SentenceTransformerEmbedder(
        "BAAI/bge-small-en-v1.5", revision="5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    )
    assert library["model"].revision == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


def test_the_thread_count_holds_in_every_thread_that_embeds(library: dict[str, Any]) -> None:
    """PyTorch's thread count is set again in each new thread that calls the
    model: the gateway calls it from a pool thread, not the one that loaded it."""
    embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    library["threads"] = None
    embedder.embed("in the loading thread")
    assert library["threads"] is None  # already set there
    worker = threading.Thread(target=embedder.embed, args=("in another",))
    worker.start()
    worker.join()
    assert library["threads"] == 1


def test_embeddings_are_asked_for_normalized_and_come_back_as_floats(
    library: dict[str, Any],
) -> None:
    embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    assert embedder.embed("what is the capital of austria") == [0.6, 0.8, 0.0]
    texts, options = library["model"].encoded[-1]
    assert texts == ["what is the capital of austria"]  # this model takes no prefix
    assert options["normalize_embeddings"] is True  # unit length: cosine is a dot product
    assert options["show_progress_bar"] is False


def test_e5_sees_the_query_prefix_it_was_trained_with(library: dict[str, Any]) -> None:
    # Both sides of a lookup are questions, so both get "query: " (e5's model
    # card, for symmetric tasks); B4 embeds both sides of a pair the same way.
    embedder = SentenceTransformerEmbedder("intfloat/e5-small-v2")
    assert embedder.embed_many(["a", "b"]) == [[0.6, 0.8, 0.0]] * 2
    assert library["model"].encoded[-1][0] == ["query: a", "query: b"]


def test_a_configured_model_without_the_install_says_how_to_add_it(
    library: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # importing it now fails
    with pytest.raises(RuntimeError, match=r"pip install '\.\[semantic\]'"):
        SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")


# --- writes in the background ------------------------------------------------------------


async def test_a_write_that_fails_in_the_background_is_logged_by_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unreachable() -> Any:
        raise OSError("connection refused")

    semantic = cache(cast(Any, unreachable))
    question = Question("scope", "what is the capital of austria")
    completion = {"choices": [], "usage": {"completion_tokens": 2}}
    with caplog.at_level(logging.WARNING, logger="tollgate.cache.semantic"):
        semantic.store("00000000-0000-0000-0000-000000000001", question, [0.0] * 384, completion)
        semantic.touch(1)
        await semantic.wait_idle()  # neither raised
    assert sorted(r.getMessage() for r in caplog.records) == [
        "semantic cache: could not count a hit: OSError",
        "semantic cache: could not store an entry: OSError",
    ]
    assert "austria" not in caplog.text  # never the prompt


# --- the verifier's wrapper --------------------------------------------------------------


class StandInCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder: an NLI model whose
    labels are in MultiNLI's order, scoring each pair as told."""

    def __init__(self, name: str, device: str, revision: str | None = None) -> None:
        self.name, self.device, self.revision = name, device, revision
        self.predicted: list[tuple[list[tuple[str, str]], dict[str, Any]]] = []
        self.dtype = "as stored"
        labels = {0: "contradiction", 1: "entailment", 2: "neutral"}
        self.model = types.SimpleNamespace(
            config=types.SimpleNamespace(id2label=labels), float=self._float
        )
        self.entail = {("a", "b"): 0.97, ("b", "a"): 0.91}

    def _float(self) -> None:
        self.dtype = "float32"

    def predict(self, pairs: list[tuple[str, str]], **options: Any) -> list[list[float]]:
        self.predicted.append((pairs, options))
        out = []
        for pair in pairs:
            p = self.entail.get(pair, 0.0)
            out.append([(1 - p) / 2, p, (1 - p) / 2])
        return out


@pytest.fixture
def cross_encoder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def make(name: str, device: str, revision: str | None = None) -> StandInCrossEncoder:
        seen["model"] = StandInCrossEncoder(name, device, revision)
        return seen["model"]

    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda n: seen.__setitem__("threads", n)  # type: ignore[attr-defined]
    st = types.ModuleType("sentence_transformers")
    st.CrossEncoder = make  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    return seen


def test_the_check_is_the_lesser_entailment_of_both_directions_in_one_call(
    cross_encoder: dict[str, Any],
) -> None:
    verifier = NLIVerifier("cross-encoder/nli-deberta-v3-xsmall", revision="abc")
    model = cross_encoder["model"]
    assert (model.device, model.revision, model.dtype) == ("cpu", "abc", "float32")
    assert cross_encoder["threads"] == 1
    assert verifier.score("a", "b") == pytest.approx(0.91)  # b entails a less surely
    pairs, options = model.predicted[-1]
    assert pairs == [("a", "b"), ("b", "a")]  # the cached question first, then reversed
    assert options["apply_softmax"] is True  # probabilities, as B4 scored them
    assert verifier.score("a", "c") == 0.0  # one direction fails: no match


def test_a_verifier_that_is_not_an_nli_model_is_refused(cross_encoder: dict[str, Any]) -> None:
    original = StandInCrossEncoder.__init__

    def sts(self: StandInCrossEncoder, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.model.config.id2label = {0: "LABEL_0"}

    StandInCrossEncoder.__init__ = sts  # type: ignore[method-assign]
    try:
        with pytest.raises(ValueError, match="not an NLI model"):
            NLIVerifier("cross-encoder/stsb-distilroberta-base")
    finally:
        StandInCrossEncoder.__init__ = original  # type: ignore[method-assign]


def test_a_configured_verifier_without_the_install_says_how_to_add_it(
    cross_encoder: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(RuntimeError, match=r"pip install '\.\[semantic\]'"):
        NLIVerifier("cross-encoder/nli-deberta-v3-xsmall")


# --- how a two-stage lookup ends -----------------------------------------------------------

TENANT = "00000000-0000-0000-0000-000000000001"
QUESTION = Question("scope", "so what is the capital of austria")
CACHED = "what is the capital of austria"
ANSWER: dict[str, Any] = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Vienna."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
}  # fmt: skip
LIMITS = Limits(queue_size=1, embed_timeout_s=0.5, verify_timeout_s=0.5)


def cache(
    sessions: Any = None,
    embedder: Any = None,
    verifier: Any = None,
    *,
    nearest: tuple[int, dict[str, Any], float, str] | Exception | None = None,
    limits: Limits = LIMITS,
) -> SemanticCache:
    """A semantic layer whose Postgres lookup returns `nearest` (or raises it)."""
    semantic = SemanticCache(
        sessions,
        embedder or WordEmbedder(),
        verifier or StandInVerifier(),
        threshold=0.68,
        verifier_threshold=0.9,
        ttl_s=60,
        limits=limits,
    )

    async def lookup(tenant_id: str, scope: str, embedding: list[float]) -> Any:
        if isinstance(nearest, Exception):
            raise nearest
        return nearest

    semantic._nearest = lookup  # type: ignore[method-assign]
    return semantic


def notes(timings: Timings) -> dict[str, str]:
    header = timings.server_timing()
    return dict(item.strip().split(";desc=") for item in header.split(",") if ";desc=" in item)


async def test_no_entry_in_scope_is_a_miss_that_may_store_the_answer() -> None:
    timings = Timings(0.0)
    found = await cache().find(TENANT, QUESTION, timings)
    assert (found.outcome, found.similarity, found.completion) == ("miss", None, None)
    assert found.embedding is not None  # stored by it afterwards
    assert notes(timings) == {"semantic": "miss"}


async def test_an_entry_below_the_threshold_is_not_checked() -> None:
    verifier = StandInVerifier()
    found = await cache(verifier=verifier, nearest=(1, ANSWER, 0.5, CACHED)).find(TENANT, QUESTION)
    assert (found.outcome, found.similarity, found.score) == ("miss", 0.5, None)
    assert found.embedding is not None
    assert verifier.calls == []


async def test_a_candidate_that_passes_the_check_is_a_hit_and_stores_nothing() -> None:
    verifier = StandInVerifier(default=0.95)
    timings = Timings(0.0)
    semantic = cache(verifier=verifier, nearest=(7, ANSWER, 0.8, CACHED))
    found = await semantic.find(TENANT, QUESTION, timings)
    assert (found.outcome, found.entry_id, found.completion) == ("hit", 7, ANSWER)
    assert (found.similarity, found.score, found.embedding) == (0.8, 0.95, None)
    assert verifier.calls == [(CACHED, QUESTION.text)]  # the cached question first
    assert notes(timings) == {"semantic": "hit"}
    durations = timings.server_timing()
    for stage in ("semantic-embed-wait", "semantic-embed", "semantic-lookup",
                  "semantic-verify-wait", "semantic-verify"):  # fmt: skip
        assert f"{stage};dur=" in durations


async def test_a_candidate_the_check_turns_down_is_rejected_and_may_be_stored() -> None:
    found = await cache(verifier=StandInVerifier(default=0.89),
                        nearest=(7, ANSWER, 0.99, CACHED)).find(TENANT, QUESTION)  # fmt: skip
    assert (found.outcome, found.completion, found.score) == ("rejected", None, 0.89)
    assert found.embedding is not None  # the lookup completed


async def test_an_entry_that_could_not_be_served_is_not_checked() -> None:
    broken = {"choices": [{"index": 0, "message": {"content": "cut"}, "finish_reason": None}]}
    verifier = StandInVerifier()
    found = await cache(verifier=verifier, nearest=(7, broken, 0.99, CACHED)).find(TENANT, QUESTION)
    assert found.outcome == "miss" and verifier.calls == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, None, True, "0.95"])
async def test_a_check_without_a_usable_score_is_an_error(bad: Any) -> None:
    verifier = StandInVerifier(default=bad)
    timings = Timings(0.0)
    found = await cache(verifier=verifier, nearest=(7, ANSWER, 0.99, CACHED)).find(
        TENANT, QUESTION, timings
    )
    assert (found.outcome, found.stage, found.completion, found.embedding) == (
        "error", "verify", None, None,
    )  # fmt: skip
    assert notes(timings) == {"semantic": "verify-error"}


async def test_a_failing_check_serves_and_stores_nothing(caplog: pytest.LogCaptureFixture) -> None:
    verifier = StandInVerifier()
    verifier.fail = True
    with caplog.at_level(logging.WARNING, logger="tollgate.cache.semantic"):
        found = await cache(verifier=verifier, nearest=(7, ANSWER, 0.99, CACHED)).find(
            TENANT, QUESTION
        )
    assert (found.outcome, found.stage, found.completion, found.embedding) == (
        "error", "verify", None, None,
    )  # fmt: skip
    assert found.similarity == 0.99
    assert "semantic cache: verify failed: RuntimeError" in caplog.text
    assert "austria" not in caplog.text  # never the prompt


async def test_a_check_that_takes_too_long_serves_and_stores_nothing() -> None:
    verifier = StandInVerifier()
    verifier.delay_s = 0.5
    limits = Limits(queue_size=1, embed_timeout_s=0.5, verify_timeout_s=0.05)
    timings = Timings(0.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    semantic = cache(verifier=verifier, nearest=(7, ANSWER, 0.99, CACHED), limits=limits)
    found = await semantic.find(TENANT, QUESTION, timings)
    assert loop.time() - started < 0.4  # didn't wait for the check
    assert (found.outcome, found.stage, found.completion, found.embedding) == (
        "timeout", "verify", None, None,
    )  # fmt: skip
    assert notes(timings) == {"semantic": "verify-timeout"}
    assert semantic.verify_pool.in_flight() == 1  # the late check still runs, unused


async def test_a_check_that_finds_its_pool_full_is_skipped() -> None:
    verifier = StandInVerifier()
    verifier.gate = threading.Event()
    limits = Limits(queue_size=0, embed_timeout_s=0.5, verify_timeout_s=5)
    semantic = cache(verifier=verifier, nearest=(7, ANSWER, 0.99, CACHED), limits=limits)
    first = asyncio.ensure_future(semantic.find(TENANT, QUESTION))
    await asyncio.to_thread(verifier.entered.wait, 5)  # the first is in its check
    timings = Timings(0.0)
    second = await semantic.find(TENANT, QUESTION, timings)
    assert (second.outcome, second.stage, second.embedding) == ("full", "verify", None)
    assert notes(timings) == {"semantic": "verify-full"}
    verifier.gate.set()
    assert (await first).outcome == "hit"
    semantic.close()


@pytest.mark.parametrize("what", ["fail", "slow", "full"])
async def test_an_embedding_that_fails_skips_the_layer(what: str) -> None:
    embedder = WordEmbedder()
    limits = LIMITS
    gate = threading.Event()
    if what == "fail":
        embedder.fail = True
    elif what == "slow":
        original = embedder.embed
        embedder.embed = lambda text: (gate.wait(5), original(text))[1]  # type: ignore[method-assign]
        limits = Limits(queue_size=0, embed_timeout_s=0.05, verify_timeout_s=0.5)
    semantic = cache(embedder=embedder, nearest=(7, ANSWER, 0.99, CACHED), limits=limits)
    if what == "full":
        semantic.embed_pool.capacity = 0  # no room at all
    found = await semantic.find(TENANT, QUESTION)
    gate.set()
    outcome = {"fail": "error", "slow": "timeout", "full": "full"}[what]
    assert (found.outcome, found.stage, found.embedding, found.similarity) == (
        outcome, "embed", None, None,
    )  # fmt: skip


async def test_a_lookup_that_fails_is_an_error_that_stores_nothing() -> None:
    found = await cache(nearest=OSError("postgres is down")).find(TENANT, QUESTION)
    assert (found.outcome, found.stage, found.embedding) == ("error", "lookup", None)


async def test_the_warm_up_runs_each_model_once_in_its_own_pool() -> None:
    embedder, verifier = WordEmbedder(), StandInVerifier()
    semantic = cache(embedder=embedder, verifier=verifier)
    await semantic.warm_up()
    assert embedder.texts == ["warm-up"]
    assert verifier.calls == [("warm up", "warm-up")]
    semantic.close()


@pytest.mark.parametrize(
    ("found", "result", "stages"),
    [
        (Found("hit", similarity=0.8, score=0.95), "hit", {"miss_path", "check"}),
        (Found("rejected", similarity=0.8, score=0.2), "rejected", {"miss_path", "check"}),
        (Found("miss", similarity=0.5), "miss", {"miss_path"}),
        (Found("full", "verify", similarity=0.8), "full", {"miss_path"}),
        (Found("timeout", "verify", similarity=0.8), "timeout", {"miss_path", "check"}),
        (Found("error", "lookup"), "error", set()),
        (Found("full", "embed"), "full", set()),
    ],
)
def test_metrics_count_each_outcome_and_time_the_stages_that_ran(
    found: Found, result: str, stages: set[str]
) -> None:
    metrics = Metrics(["gpt-4o-mini"], ["mock"])
    timings = Timings(0.0)
    for stage, seconds in (("embed-wait", 0.001), ("embed", 0.01), ("lookup", 0.004),
                           ("verify-wait", 0.02), ("verify", 0.09)):  # fmt: skip
        timings.add(f"semantic-{stage}", seconds)
    metrics.semantic_lookup(found, timings)
    text = metrics.exposition().decode()
    assert f'tollgate_cache_lookups_total{{layer="semantic",result="{result}"}} 1.0' in text
    for stage in ("miss_path", "check"):
        count = f'tollgate_semantic_cache_seconds_count{{stage="{stage}"}} 1.0'
        assert (count in text) == (stage in stages), stage
    if "miss_path" in stages:  # embedding, its wait and the lookup: 15 ms
        line = next(
            x
            for x in text.splitlines()
            if x.startswith('tollgate_semantic_cache_seconds_sum{stage="miss_path"}')
        )
        assert float(line.split()[-1]) == pytest.approx(0.015)
