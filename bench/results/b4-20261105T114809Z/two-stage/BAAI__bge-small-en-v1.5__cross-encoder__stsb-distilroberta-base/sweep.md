### B4 two stages: `BAAI/bge-small-en-v1.5` then `cross-encoder/stsb-distilroberta-base`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

Stage 1, the embedding model at its loose threshold 0.79 (the highest that keeps at least 95% of QQP duplicates): QQP recall 95.4% (2,385/2,500), hard-positive recall 89.8% (44/49). It passes 39.04% (976/2,500) of QQP non-duplicates and 86.91% (166/191) of near-misses on to the cross-encoder; a request whose candidate the cross-encoder rejects pays for the check and then misses.

**No threshold from 0.05 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.05 | 39.04% (976/2,500) | 95.4% (2,385/2,500) | 86.91% (166/191) | 89.8% (44/49) |
| 0.10 | 39.00% (975/2,500) | 95.4% (2,385/2,500) | 86.91% (166/191) | 89.8% (44/49) |
| 0.15 | 39.00% (975/2,500) | 95.4% (2,385/2,500) | 86.91% (166/191) | 89.8% (44/49) |
| 0.20 | 38.92% (973/2,500) | 95.3% (2,382/2,500) | 85.34% (163/191) | 89.8% (44/49) |
| 0.25 | 38.80% (970/2,500) | 95.2% (2,379/2,500) | 84.29% (161/191) | 89.8% (44/49) |
| 0.30 | 38.20% (955/2,500) | 95.0% (2,374/2,500) | 82.20% (157/191) | 89.8% (44/49) |
| 0.35 | 37.08% (927/2,500) | 94.2% (2,356/2,500) | 79.06% (151/191) | 89.8% (44/49) |
| 0.40 | 35.52% (888/2,500) | 92.9% (2,323/2,500) | 71.73% (137/191) | 87.8% (43/49) |
| 0.45 | 33.52% (838/2,500) | 91.1% (2,277/2,500) | 63.35% (121/191) | 85.7% (42/49) |
| 0.50 | 31.16% (779/2,500) | 88.5% (2,212/2,500) | 53.40% (102/191) | 75.5% (37/49) |
| 0.55 | 28.04% (701/2,500) | 85.1% (2,128/2,500) | 49.21% (94/191) | 67.3% (33/49) |
| 0.60 | 24.16% (604/2,500) | 80.4% (2,010/2,500) | 47.12% (90/191) | 63.3% (31/49) |
| 0.65 | 20.44% (511/2,500) | 73.8% (1,844/2,500) | 42.93% (82/191) | 46.9% (23/49) |
| 0.70 | 16.84% (421/2,500) | 66.2% (1,654/2,500) | 39.79% (76/191) | 42.9% (21/49) |
| 0.75 | 13.00% (325/2,500) | 57.8% (1,444/2,500) | 36.13% (69/191) | 32.7% (16/49) |
| 0.80 | 9.44% (236/2,500) | 48.8% (1,220/2,500) | 29.32% (56/191) | 28.6% (14/49) |
| 0.85 | 6.00% (150/2,500) | 40.6% (1,014/2,500) | 26.18% (50/191) | 16.3% (8/49) |
| 0.90 | 3.76% (94/2,500) | 32.3% (807/2,500) | 22.51% (43/191) | 6.1% (3/49) |
| 0.95 | 2.04% (51/2,500) | 22.8% (571/2,500) | 17.80% (34/191) | 4.1% (2/49) |
| 0.96 | 1.80% (45/2,500) | 20.3% (508/2,500) | 16.75% (32/191) | 2.0% (1/49) |
| 0.97 | 1.48% (37/2,500) | 18.5% (462/2,500) | 15.18% (29/191) | 0.0% (0/49) |
| 0.98 | 1.04% (26/2,500) | 14.3% (358/2,500) | 13.61% (26/191) | 0.0% (0/49) |
| 0.99 | 0.28% (7/2,500) | 3.3% (83/2,500) | 5.76% (11/191) | 0.0% (0/49) |

False hits at 0.99, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 0.28% (7/2,500) |
| Adversarial | number | 0.00% (0/22) |
| Adversarial | entity | 0.00% (0/22) |
| Adversarial | action | 9.09% (2/22) |
| Adversarial | negation | 0.00% (0/18) |
| Adversarial | unit | 11.11% (2/18) |
| Adversarial | year | 0.00% (0/18) |
| Adversarial | comparison | 0.00% (0/20) |
| Adversarial | technical | 4.76% (1/21) |
| Adversarial | reversal | 42.86% (6/14) |
| Adversarial | scope | 0.00% (0/16) |
| Adversarial | all near-misses | 5.76% (11/191) |

Recall at 0.99, by category (pairs that should hit):

| Set | Category | Recall |
|---|---|---|
| QQP | duplicates | 3.3% (83/2,500) |
| Adversarial | paraphrase | 0.0% (0/49) |

The near-misses that hit at 0.99, 11 of 191:

| Pair | Category | Score | Question | Near-miss |
|---|---|---|---|---|
| adv-175 | reversal | 0.991 | How do I convert Celsius to Fahrenheit? | How do I convert Fahrenheit to Celsius? |
| adv-170 | reversal | 0.991 | How do I migrate from MySQL to PostgreSQL? | How do I migrate from PostgreSQL to MySQL? |
| adv-174 | reversal | 0.991 | How do I cast a float to an int in C? | How do I cast an int to a float in C? |
| adv-48 | action | 0.991 | How do I encrypt a file with GPG? | How do I decrypt a file with GPG? |
| adv-173 | reversal | 0.991 | How do I convert a PNG to a JPG? | How do I convert a JPG to a PNG? |
| adv-165 | reversal | 0.991 | How do I convert JSON to YAML? | How do I convert YAML to JSON? |
| adv-172 | reversal | 0.991 | How do I switch from Android to iPhone? | How do I switch from iPhone to Android? |
| adv-88 | unit | 0.990 | What is 30 degrees Celsius in Fahrenheit? | What is 30 degrees Fahrenheit in Celsius? |
| adv-59 | action | 0.990 | How do I upload a file to S3 with the AWS CLI? | How do I download a file from S3 with the AWS CLI? |
| adv-93 | unit | 0.990 | What is 100 km/h in mph? | What is 100 mph in km/h? |
| adv-145 | technical | 0.990 | How do I center a div horizontally in CSS? | How do I center a div vertically in CSS? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.05 | 21 | 13 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 14 |
| 0.10 | 21 | 13 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 14 |
| 0.15 | 21 | 13 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 14 |
| 0.20 | 21 | 10 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 14 |
| 0.25 | 21 | 8 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 14 |
| 0.30 | 21 | 6 | 21 | 18 | 15 | 13 | 18 | 20 | 13 | 12 |
| 0.35 | 20 | 5 | 20 | 17 | 14 | 12 | 18 | 20 | 13 | 12 |
| 0.40 | 18 | 4 | 19 | 12 | 14 | 12 | 15 | 19 | 13 | 11 |
| 0.45 | 12 | 3 | 18 | 12 | 13 | 10 | 13 | 18 | 13 | 9 |
| 0.50 | 8 | 3 | 14 | 11 | 13 | 4 | 12 | 16 | 12 | 9 |
| 0.55 | 7 | 3 | 14 | 9 | 12 | 3 | 11 | 14 | 12 | 9 |
| 0.60 | 6 | 3 | 13 | 9 | 12 | 3 | 11 | 12 | 12 | 9 |
| 0.65 | 4 | 3 | 13 | 8 | 11 | 3 | 9 | 12 | 12 | 7 |
| 0.70 | 1 | 2 | 13 | 8 | 11 | 3 | 9 | 12 | 12 | 5 |
| 0.75 | 0 | 2 | 12 | 8 | 11 | 2 | 8 | 10 | 12 | 4 |
| 0.80 | 0 | 1 | 10 | 5 | 11 | 2 | 5 | 8 | 12 | 2 |
| 0.85 | 0 | 1 | 10 | 5 | 9 | 1 | 5 | 6 | 12 | 1 |
| 0.90 | 0 | 1 | 10 | 5 | 7 | 1 | 3 | 4 | 12 | 0 |
| 0.95 | 0 | 1 | 6 | 3 | 7 | 1 | 2 | 2 | 12 | 0 |
| 0.96 | 0 | 1 | 6 | 2 | 7 | 1 | 2 | 2 | 11 | 0 |
| 0.97 | 0 | 0 | 6 | 2 | 6 | 1 | 2 | 1 | 11 | 0 |
| 0.98 | 0 | 0 | 5 | 1 | 5 | 1 | 2 | 1 | 11 | 0 |
| 0.99 | 0 | 0 | 2 | 0 | 2 | 0 | 0 | 1 | 6 | 0 |
