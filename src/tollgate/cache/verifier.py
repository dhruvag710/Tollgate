"""The semantic cache's second stage (SPEC Phase 2.4): a cross-encoder reads the
cached question and the new one together and decides whether they ask the
same thing.

B4 (BENCHMARKS.md) found that no embedding model alone keeps false hits within
target, because questions with the same words in a different order embed
almost identically, and that an NLI cross-encoder checking the embedding
model's candidate does. The check is a match only if each question entails
the other: its score is the lesser of the two entailment probabilities, both
directions scored in one call, as bench/cache_eval/two_stage.py measured it.

The gateway needs only the Verifier protocol. The real one is part of the
optional install (`.[semantic]`), imported only when a verifier is
configured; tests use a stand-in (tests/helpers.py).
"""

import threading
from typing import Any, Protocol


class Verifier(Protocol):
    def score(self, cached: str, query: str) -> float:
        """How surely the two are the same question, 0 to 1. Blocking: call it
        off the event loop."""
        ...


class NLIVerifier:
    """An NLI cross-encoder on the CPU, in float32.

    `threads` caps PyTorch's own threads, as for the embedding model
    (embedder.SentenceTransformerEmbedder), and is applied in each thread
    that calls the model. Calls are made one at a time: the model's tokenizer
    refuses to be used by two threads at once.
    """

    def __init__(self, model_name: str, *, revision: str | None = None, threads: int = 1) -> None:
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "a semantic cache verifier is configured, but sentence-transformers isn't "
                "installed: pip install '.[semantic]'"
            ) from exc
        self._torch: Any = torch
        self._threads = threads
        self._configured = threading.local()
        self._use_threads()
        self.model_name = model_name
        self._model: Any = CrossEncoder(model_name, device="cpu", revision=revision)
        self._model.model.float()  # as B4 ran it, whatever the stored precision
        labels = {str(v).lower(): int(k) for k, v in self._model.model.config.id2label.items()}
        if "entailment" not in labels:
            raise ValueError(f"{model_name} is not an NLI model: its labels are {sorted(labels)}")
        self._entailment = labels["entailment"]
        self._lock = threading.Lock()

    def _use_threads(self) -> None:
        if not getattr(self._configured, "done", False):
            self._torch.set_num_threads(self._threads)
            self._configured.done = True

    def score(self, cached: str, query: str) -> float:
        with self._lock:
            self._use_threads()
            both = self._model.predict(
                [(cached, query), (query, cached)],
                apply_softmax=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        return float(min(both[0][self._entailment], both[1][self._entailment]))
