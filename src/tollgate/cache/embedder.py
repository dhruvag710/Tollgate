"""Text embeddings for the semantic cache (SPEC Phase 2.4).

The gateway needs only the Embedder protocol. The real one runs a
sentence-transformers model on the CPU, in the gateway process; it is an
optional install (`.[semantic]`), imported only when a model is configured.
Tests use a deterministic stand-in instead (tests/helpers.py).
"""

import threading
from typing import Any, Protocol

# Text some models were trained to see in front of a query (e5's "query: ").
QUERY_PREFIXES = {"intfloat/e5-small-v2": "query: "}


class Embedder(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]:
        """The text's embedding, unit length. Blocking: call it off the event loop."""
        ...


class SentenceTransformerEmbedder:
    """A sentence-transformers model on the CPU.

    `threads` caps PyTorch's own threads: one by default, as a gateway replica
    runs on one CPU, and B4 measures latency the same way. It is applied in
    each thread that calls the model (the gateway calls it from a pool of its
    own, cache.pool). Calls are made one at a time: Hugging Face's fast
    tokenizers refuse to be used by two threads at once. `revision` pins the
    model's commit on the Hugging Face Hub (the one B4 measured).
    """

    def __init__(self, model_name: str, *, revision: str | None = None, threads: int = 1) -> None:
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "a semantic cache model is configured, but sentence-transformers isn't "
                "installed: pip install '.[semantic]'"
            ) from exc
        self._torch: Any = torch
        self._threads = threads
        self._configured = threading.local()
        self._use_threads()
        self.model_name = model_name
        # Float32: a model stored in float16 (gte-small) otherwise runs in
        # float16, which a CPU computes several times slower. The upcast is exact.
        self._model: Any = SentenceTransformer(model_name, device="cpu", revision=revision).float()
        dimension = getattr(self._model, "get_embedding_dimension", None)
        dimensions = (dimension or self._model.get_sentence_embedding_dimension)()
        assert isinstance(dimensions, int)
        self.dimensions = dimensions
        self._prefix = QUERY_PREFIXES.get(model_name, "")
        self._lock = threading.Lock()

    def _use_threads(self) -> None:
        if not getattr(self._configured, "done", False):
            self._torch.set_num_threads(self._threads)
            self._configured.done = True

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        with self._lock:
            self._use_threads()
            vectors = self._model.encode(
                [self._prefix + t for t in texts],
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return [list(map(float, v)) for v in vectors]
