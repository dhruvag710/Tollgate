### B4: two stages, an embedding model then a cross-encoder

Stage 1 passes a pair at the embedding model's loose threshold, the highest that keeps at least 95% of QQP duplicates; stage 2 hits at the cross-encoder's threshold, the lowest with false hits at or below 2% on both sets. Embed p99 is the miss path's share before the Postgres lookup (budget 25 ms for both; B5 times the lookup). The hit path is embed then verify, 500 queries on one PyTorch thread after 20 to warm up.

| Embedding model | Cross-encoder | Loose threshold | Stage-1 hard-positive recall | Cross-encoder threshold | QQP false hits | Near-miss false hits | QQP recall | Hard-positive recall | Embed p99 (ms) | Hit path p50 / p99 (ms) |
|---|---|---|---|---|---|---|---|---|---|---|
| bge-small-en-v1.5 | nli-deberta-v3-xsmall | 0.79 | 89.8% (44/49) | 0.95 | 1.20% (30/2,500) | 1.05% (2/191) | 18.6% (464/2,500) | 20.4% (10/49) | 37.2 | 106.5 / 337.3 |
| **all-MiniLM-L6-v2** | nli-deberta-v3-xsmall | 0.68 | 85.7% (42/49) | 0.90 | 1.64% (41/2,500) | 1.57% (3/191) | 24.4% (610/2,500) | 42.9% (21/49) | 18.5 | 98.5 / 309.0 |
| e5-small-v2 | nli-deberta-v3-xsmall | 0.90 | 87.8% (43/49) | 0.95 | 1.24% (31/2,500) | 1.05% (2/191) | 18.6% (464/2,500) | 20.4% (10/49) | 70.8 | 111.3 / 367.6 |
| gte-small | nli-deberta-v3-xsmall | 0.89 | 89.8% (44/49) | 0.90 | 1.68% (42/2,500) | 1.57% (3/191) | 24.3% (607/2,500) | 42.9% (21/49) | 33.5 | 106.9 / 380.4 |
| bge-small-en-v1.5 | stsb-distilroberta-base | 0.79 | 89.8% (44/49) | none meets 2% | n/a | n/a | n/a | n/a | 37.2 | 63.5 / 244.6 |
| all-MiniLM-L6-v2 | stsb-distilroberta-base | 0.68 | 85.7% (42/49) | none meets 2% | n/a | n/a | n/a | n/a | 18.5 | 52.9 / 168.5 |
| e5-small-v2 | stsb-distilroberta-base | 0.90 | 87.8% (43/49) | none meets 2% | n/a | n/a | n/a | n/a | 70.8 | 62.0 / 203.3 |
| gte-small | stsb-distilroberta-base | 0.89 | 89.8% (44/49) | none meets 2% | n/a | n/a | n/a | n/a | 33.5 | 63.9 / 234.0 |

Best pairing meeting both targets: `sentence-transformers/all-MiniLM-L6-v2` then `cross-encoder/nli-deberta-v3-xsmall` at 0.90: the highest QQP recall, or within 1 percentage point of it, the fastest hit path.

| Cross-encoder | Trained on | Verify p50 / p99 (ms) | Memory (MB) | Load (s) | Scoring every pair (s) |
|---|---|---|---|---|---|
| cross-encoder/nli-deberta-v3-xsmall | SNLI, MultiNLI | 88.7 / 284.3 | 402 | 0.9 | 211 |
| cross-encoder/stsb-distilroberta-base | STS benchmark | 41.3 / 88.0 | 349 | 0.5 | 129 |

False hits and recall by category, at each pairing's chosen threshold:

| Pairs | bge-small-en-v1.5 + nli-deberta-v3-xsmall at 0.95 | all-MiniLM-L6-v2 + nli-deberta-v3-xsmall at 0.90 | e5-small-v2 + nli-deberta-v3-xsmall at 0.95 | gte-small + nli-deberta-v3-xsmall at 0.90 | bge-small-en-v1.5 + stsb-distilroberta-base (none chosen; at 0.99) | all-MiniLM-L6-v2 + stsb-distilroberta-base (none chosen; at 0.99) | e5-small-v2 + stsb-distilroberta-base (none chosen; at 0.99) | gte-small + stsb-distilroberta-base (none chosen; at 0.99) |
|---|---|---|---|---|---|---|---|---|
| False hits: QQP non-duplicates | 1.20% (30/2,500) | 1.64% (41/2,500) | 1.24% (31/2,500) | 1.68% (42/2,500) | 0.28% (7/2,500) | 0.28% (7/2,500) | 0.28% (7/2,500) | 0.28% (7/2,500) |
| False hits: number | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 4.55% (1/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) |
| False hits: entity | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) |
| False hits: action | 4.55% (1/22) | 4.55% (1/22) | 4.55% (1/22) | 4.55% (1/22) | 9.09% (2/22) | 9.09% (2/22) | 9.09% (2/22) | 9.09% (2/22) |
| False hits: negation | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) |
| False hits: unit | 0.00% (0/18) | 5.56% (1/18) | 0.00% (0/18) | 0.00% (0/18) | 11.11% (2/18) | 11.11% (2/18) | 11.11% (2/18) | 11.11% (2/18) |
| False hits: year | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) |
| False hits: comparison | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) |
| False hits: technical | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) | 4.76% (1/21) |
| False hits: reversal | 0.00% (0/14) | 0.00% (0/14) | 0.00% (0/14) | 0.00% (0/14) | 42.86% (6/14) | 42.86% (6/14) | 42.86% (6/14) | 42.86% (6/14) |
| False hits: scope | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) |
| False hits: all near-misses | 1.05% (2/191) | 1.57% (3/191) | 1.05% (2/191) | 1.57% (3/191) | 5.76% (11/191) | 5.76% (11/191) | 5.76% (11/191) | 5.76% (11/191) |
| Recall: QQP duplicates | 18.6% (464/2,500) | 24.4% (610/2,500) | 18.6% (464/2,500) | 24.3% (607/2,500) | 3.3% (83/2,500) | 3.3% (83/2,500) | 3.3% (83/2,500) | 3.3% (83/2,500) |
| Recall: paraphrase | 20.4% (10/49) | 42.9% (21/49) | 20.4% (10/49) | 42.9% (21/49) | 0.0% (0/49) | 0.0% (0/49) | 0.0% (0/49) | 0.0% (0/49) |

Timing runs flagged (machine.Watch): 
- cross-encoder/nli-deberta-v3-xsmall, verifying: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/nli-deberta-v3-xsmall, hit path with bge-small-en-v1.5: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/nli-deberta-v3-xsmall, hit path with all-MiniLM-L6-v2: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/nli-deberta-v3-xsmall, hit path with e5-small-v2: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/nli-deberta-v3-xsmall, hit path with gte-small: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/stsb-distilroberta-base, hit path with bge-small-en-v1.5: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/stsb-distilroberta-base, hit path with all-MiniLM-L6-v2: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/stsb-distilroberta-base, hit path with e5-small-v2: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
- cross-encoder/stsb-distilroberta-base, hit path with gte-small: clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz
