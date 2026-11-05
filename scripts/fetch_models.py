"""Download the semantic cache's models into the Hugging Face cache, each at a
pinned revision, so a gateway with the layer on never downloads anything at
startup (SPEC Phase 2.4). The Dockerfile's semantic build runs it:

    python scripts/fetch_models.py <repo>@<commit> [<repo>@<commit> ...]

The revisions are the ones B4 measured (BENCHMARKS.md); the gateway loads the
same ones (SEMANTIC_CACHE_MODEL_REVISION, SEMANTIC_CACHE_VERIFIER_REVISION).
Only the files a model needs on the CPU are fetched: its weights in
safetensors, its configs and its tokenizer, not the ONNX, OpenVINO or other
framework copies some repositories also carry.
"""

import sys

# Configs and tokenizers (*.json, vocab.txt, spm.model) and the weights.
FILES = ["*.json", "*.txt", "*.model", "model.safetensors"]


def main(items: list[str]) -> None:
    from huggingface_hub import snapshot_download

    if not items:
        raise SystemExit(__doc__)
    for item in items:
        repo, _, revision = item.partition("@")
        if len(revision) != 40:
            raise SystemExit(f"{item}: pin a full commit (<repo>@<40-character sha>)")
        path = snapshot_download(repo, revision=revision, allow_patterns=FILES)
        print(f"{repo} at {revision}: {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
