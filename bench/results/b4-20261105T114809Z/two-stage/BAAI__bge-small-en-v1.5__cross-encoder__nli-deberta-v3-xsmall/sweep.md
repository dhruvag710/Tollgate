### B4 two stages: `BAAI/bge-small-en-v1.5` then `cross-encoder/nli-deberta-v3-xsmall`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

Stage 1, the embedding model at its loose threshold 0.79 (the highest that keeps at least 95% of QQP duplicates): QQP recall 95.4% (2,385/2,500), hard-positive recall 89.8% (44/49). It passes 39.04% (976/2,500) of QQP non-duplicates and 86.91% (166/191) of near-misses on to the cross-encoder; a request whose candidate the cross-encoder rejects pays for the check and then misses.

**Threshold chosen: 0.95**, the lowest with false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.05 | 8.48% (212/2,500) | 48.6% (1,215/2,500) | 5.76% (11/191) | 71.4% (35/49) |
| 0.10 | 6.64% (166/2,500) | 43.6% (1,091/2,500) | 5.24% (10/191) | 69.4% (34/49) |
| 0.15 | 5.44% (136/2,500) | 41.1% (1,027/2,500) | 4.19% (8/191) | 69.4% (34/49) |
| 0.20 | 5.00% (125/2,500) | 39.5% (988/2,500) | 3.66% (7/191) | 65.3% (32/49) |
| 0.25 | 4.64% (116/2,500) | 38.2% (956/2,500) | 3.66% (7/191) | 65.3% (32/49) |
| 0.30 | 4.40% (110/2,500) | 37.5% (938/2,500) | 3.66% (7/191) | 61.2% (30/49) |
| 0.35 | 4.28% (107/2,500) | 36.4% (911/2,500) | 3.14% (6/191) | 61.2% (30/49) |
| 0.40 | 4.08% (102/2,500) | 35.5% (888/2,500) | 3.14% (6/191) | 59.2% (29/49) |
| 0.45 | 3.80% (95/2,500) | 34.9% (872/2,500) | 3.14% (6/191) | 59.2% (29/49) |
| 0.50 | 3.44% (86/2,500) | 34.0% (851/2,500) | 2.62% (5/191) | 59.2% (29/49) |
| 0.55 | 3.32% (83/2,500) | 33.4% (835/2,500) | 2.62% (5/191) | 55.1% (27/49) |
| 0.60 | 3.12% (78/2,500) | 32.6% (816/2,500) | 2.62% (5/191) | 53.1% (26/49) |
| 0.65 | 3.00% (75/2,500) | 32.0% (801/2,500) | 2.62% (5/191) | 53.1% (26/49) |
| 0.70 | 2.84% (71/2,500) | 31.2% (781/2,500) | 2.62% (5/191) | 53.1% (26/49) |
| 0.75 | 2.64% (66/2,500) | 29.8% (745/2,500) | 2.62% (5/191) | 53.1% (26/49) |
| 0.80 | 2.52% (63/2,500) | 28.9% (723/2,500) | 2.62% (5/191) | 51.0% (25/49) |
| 0.85 | 2.20% (55/2,500) | 27.4% (684/2,500) | 2.62% (5/191) | 44.9% (22/49) |
| 0.90 | 1.64% (41/2,500) | 24.4% (609/2,500) | 2.09% (4/191) | 42.9% (21/49) |
| 0.95 **(chosen)** | 1.20% (30/2,500) | 18.6% (464/2,500) | 1.05% (2/191) | 20.4% (10/49) |
| 0.96 | 0.76% (19/2,500) | 16.8% (419/2,500) | 1.05% (2/191) | 14.3% (7/49) |
| 0.97 | 0.40% (10/2,500) | 12.9% (322/2,500) | 0.52% (1/191) | 10.2% (5/49) |
| 0.98 | 0.28% (7/2,500) | 8.1% (203/2,500) | 0.00% (0/191) | 4.1% (2/49) |
| 0.99 | 0.04% (1/2,500) | 1.5% (38/2,500) | 0.00% (0/191) | 0.0% (0/49) |

False hits at 0.95, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 1.20% (30/2,500) |
| Adversarial | number | 0.00% (0/22) |
| Adversarial | entity | 0.00% (0/22) |
| Adversarial | action | 4.55% (1/22) |
| Adversarial | negation | 0.00% (0/18) |
| Adversarial | unit | 0.00% (0/18) |
| Adversarial | year | 0.00% (0/18) |
| Adversarial | comparison | 0.00% (0/20) |
| Adversarial | technical | 4.76% (1/21) |
| Adversarial | reversal | 0.00% (0/14) |
| Adversarial | scope | 0.00% (0/16) |
| Adversarial | all near-misses | 1.05% (2/191) |

Recall at 0.95, by category (pairs that should hit):

| Set | Category | Recall |
|---|---|---|
| QQP | duplicates | 18.6% (464/2,500) |
| Adversarial | paraphrase | 20.4% (10/49) |

The near-misses that hit at 0.95, 2 of 191:

| Pair | Category | Score | Question | Near-miss |
|---|---|---|---|---|
| adv-157 | technical | 0.979 | How do I undo the last git commit? | How do I undo the last git push? |
| adv-52 | action | 0.965 | How do I zip a folder on macOS? | How do I unzip a folder on macOS? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.05 | 1 | 1 | 3 | 0 | 3 | 0 | 0 | 3 | 0 | 0 |
| 0.10 | 1 | 1 | 3 | 0 | 3 | 0 | 0 | 2 | 0 | 0 |
| 0.15 | 1 | 0 | 3 | 0 | 2 | 0 | 0 | 2 | 0 | 0 |
| 0.20 | 1 | 0 | 2 | 0 | 2 | 0 | 0 | 2 | 0 | 0 |
| 0.25 | 1 | 0 | 2 | 0 | 2 | 0 | 0 | 2 | 0 | 0 |
| 0.30 | 1 | 0 | 2 | 0 | 2 | 0 | 0 | 2 | 0 | 0 |
| 0.35 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 2 | 0 | 0 |
| 0.40 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 2 | 0 | 0 |
| 0.45 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 2 | 0 | 0 |
| 0.50 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.55 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.60 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.65 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.70 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.75 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.80 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.85 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.90 | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| 0.95 **(chosen)** | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| 0.96 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| 0.97 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| 0.98 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 0.99 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
