### B4 two stages: `intfloat/e5-small-v2` then `cross-encoder/nli-deberta-v3-xsmall`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

Stage 1, the embedding model at its loose threshold 0.90 (the highest that keeps at least 95% of QQP duplicates): QQP recall 97.1% (2,427/2,500), hard-positive recall 87.8% (43/49). It passes 44.20% (1,105/2,500) of QQP non-duplicates and 93.19% (178/191) of near-misses on to the cross-encoder; a request whose candidate the cross-encoder rejects pays for the check and then misses.

**Threshold chosen: 0.95**, the lowest with false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.05 | 8.92% (223/2,500) | 48.8% (1,219/2,500) | 6.28% (12/191) | 71.4% (35/49) |
| 0.10 | 7.04% (176/2,500) | 43.8% (1,095/2,500) | 5.76% (11/191) | 71.4% (35/49) |
| 0.15 | 5.68% (142/2,500) | 41.2% (1,031/2,500) | 4.19% (8/191) | 71.4% (35/49) |
| 0.20 | 5.20% (130/2,500) | 39.7% (992/2,500) | 3.66% (7/191) | 67.3% (33/49) |
| 0.25 | 4.76% (119/2,500) | 38.4% (960/2,500) | 3.66% (7/191) | 67.3% (33/49) |
| 0.30 | 4.52% (113/2,500) | 37.7% (942/2,500) | 3.66% (7/191) | 63.3% (31/49) |
| 0.35 | 4.40% (110/2,500) | 36.6% (915/2,500) | 3.14% (6/191) | 63.3% (31/49) |
| 0.40 | 4.20% (105/2,500) | 35.7% (892/2,500) | 3.14% (6/191) | 61.2% (30/49) |
| 0.45 | 3.92% (98/2,500) | 35.0% (876/2,500) | 3.14% (6/191) | 61.2% (30/49) |
| 0.50 | 3.52% (88/2,500) | 34.2% (854/2,500) | 2.62% (5/191) | 61.2% (30/49) |
| 0.55 | 3.40% (85/2,500) | 33.5% (838/2,500) | 2.62% (5/191) | 57.1% (28/49) |
| 0.60 | 3.20% (80/2,500) | 32.8% (819/2,500) | 2.62% (5/191) | 55.1% (27/49) |
| 0.65 | 3.04% (76/2,500) | 32.2% (804/2,500) | 2.62% (5/191) | 55.1% (27/49) |
| 0.70 | 2.84% (71/2,500) | 31.4% (784/2,500) | 2.62% (5/191) | 55.1% (27/49) |
| 0.75 | 2.64% (66/2,500) | 29.8% (746/2,500) | 2.62% (5/191) | 55.1% (27/49) |
| 0.80 | 2.52% (63/2,500) | 28.9% (723/2,500) | 2.62% (5/191) | 53.1% (26/49) |
| 0.85 | 2.20% (55/2,500) | 27.4% (684/2,500) | 2.62% (5/191) | 46.9% (23/49) |
| 0.90 | 1.68% (42/2,500) | 24.4% (609/2,500) | 2.09% (4/191) | 42.9% (21/49) |
| 0.95 **(chosen)** | 1.24% (31/2,500) | 18.6% (464/2,500) | 1.05% (2/191) | 20.4% (10/49) |
| 0.96 | 0.80% (20/2,500) | 16.8% (419/2,500) | 1.05% (2/191) | 14.3% (7/49) |
| 0.97 | 0.44% (11/2,500) | 12.9% (322/2,500) | 0.52% (1/191) | 10.2% (5/49) |
| 0.98 | 0.32% (8/2,500) | 8.1% (203/2,500) | 0.00% (0/191) | 4.1% (2/49) |
| 0.99 | 0.04% (1/2,500) | 1.5% (38/2,500) | 0.00% (0/191) | 0.0% (0/49) |

False hits at 0.95, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 1.24% (31/2,500) |
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
| 0.05 | 1 | 1 | 3 | 0 | 4 | 0 | 0 | 3 | 0 | 0 |
| 0.10 | 1 | 1 | 3 | 0 | 4 | 0 | 0 | 2 | 0 | 0 |
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
