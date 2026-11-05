"""The semantic layer's parts that need no Postgres (SPEC Phase 2.4): the
question a request asks it, the wrapper around the real model (driven here by
a stand-in for sentence-transformers, which CI doesn't install), and writes in
the background that fail."""

import logging
import sys
import types
from typing import Any, cast

import pytest

from tests.helpers import WordEmbedder
from tollgate.cache.embedder import SentenceTransformerEmbedder
from tollgate.cache.policy import exact_key
from tollgate.cache.semantic import Question, SemanticCache, question_of

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

    def __init__(self, name: str, device: str) -> None:
        self.name, self.device = name, device
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

    def model(name: str, device: str) -> StandInModel:
        seen["model"] = StandInModel(name, device)
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
    SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5", threads=4)
    assert library["threads"] == 4


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

    semantic = SemanticCache(cast(Any, unreachable), WordEmbedder(), threshold=0.9, ttl_s=60)
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
