### B4 two stages: `sentence-transformers/all-MiniLM-L6-v2` then `cross-encoder/stsb-distilroberta-base`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

Stage 1, the embedding model at its loose threshold 0.68 (the highest that keeps at least 95% of QQP duplicates): QQP recall 95.5% (2,387/2,500), hard-positive recall 85.7% (42/49). It passes 39.16% (979/2,500) of QQP non-duplicates and 91.10% (174/191) of near-misses on to the cross-encoder; a request whose candidate the cross-encoder rejects pays for the check and then misses.

**No threshold from 0.05 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.05 | 39.16% (979/2,500) | 95.5% (2,387/2,500) | 91.10% (174/191) | 85.7% (42/49) |
| 0.10 | 39.12% (978/2,500) | 95.5% (2,387/2,500) | 91.10% (174/191) | 85.7% (42/49) |
| 0.15 | 39.12% (978/2,500) | 95.5% (2,387/2,500) | 91.10% (174/191) | 85.7% (42/49) |
| 0.20 | 39.08% (977/2,500) | 95.4% (2,384/2,500) | 89.01% (170/191) | 85.7% (42/49) |
| 0.25 | 38.96% (974/2,500) | 95.2% (2,380/2,500) | 87.96% (168/191) | 85.7% (42/49) |
| 0.30 | 38.24% (956/2,500) | 94.9% (2,373/2,500) | 85.34% (163/191) | 83.7% (41/49) |
| 0.35 | 37.12% (928/2,500) | 94.2% (2,355/2,500) | 80.63% (154/191) | 81.6% (40/49) |
| 0.40 | 35.60% (890/2,500) | 92.7% (2,318/2,500) | 71.73% (137/191) | 79.6% (39/49) |
| 0.45 | 33.56% (839/2,500) | 90.8% (2,271/2,500) | 61.26% (117/191) | 79.6% (39/49) |
| 0.50 | 31.44% (786/2,500) | 88.2% (2,204/2,500) | 51.83% (99/191) | 69.4% (34/49) |
| 0.55 | 28.28% (707/2,500) | 84.7% (2,118/2,500) | 47.64% (91/191) | 63.3% (31/49) |
| 0.60 | 24.36% (609/2,500) | 80.1% (2,003/2,500) | 46.60% (89/191) | 59.2% (29/49) |
| 0.65 | 20.60% (515/2,500) | 73.6% (1,841/2,500) | 42.41% (81/191) | 42.9% (21/49) |
| 0.70 | 16.80% (420/2,500) | 66.2% (1,655/2,500) | 38.74% (74/191) | 40.8% (20/49) |
| 0.75 | 13.08% (327/2,500) | 57.8% (1,446/2,500) | 35.60% (68/191) | 32.7% (16/49) |
| 0.80 | 9.52% (238/2,500) | 48.7% (1,218/2,500) | 28.80% (55/191) | 28.6% (14/49) |
| 0.85 | 6.08% (152/2,500) | 40.5% (1,013/2,500) | 25.65% (49/191) | 16.3% (8/49) |
| 0.90 | 3.80% (95/2,500) | 32.3% (807/2,500) | 21.99% (42/191) | 6.1% (3/49) |
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
| 0.05 | 21 | 16 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 16 |
| 0.10 | 21 | 16 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 16 |
| 0.15 | 21 | 16 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 16 |
| 0.20 | 21 | 12 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 16 |
| 0.25 | 21 | 11 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 15 |
| 0.30 | 21 | 8 | 20 | 18 | 17 | 18 | 20 | 16 | 12 | 13 |
| 0.35 | 20 | 7 | 19 | 17 | 16 | 16 | 19 | 16 | 12 | 12 |
| 0.40 | 17 | 5 | 18 | 12 | 16 | 15 | 16 | 15 | 12 | 11 |
| 0.45 | 11 | 3 | 17 | 12 | 15 | 11 | 13 | 14 | 12 | 9 |
| 0.50 | 7 | 3 | 13 | 11 | 15 | 4 | 12 | 13 | 12 | 9 |
| 0.55 | 6 | 3 | 13 | 9 | 13 | 3 | 11 | 12 | 12 | 9 |
| 0.60 | 5 | 3 | 12 | 9 | 13 | 3 | 11 | 12 | 12 | 9 |
| 0.65 | 3 | 3 | 12 | 8 | 12 | 3 | 9 | 12 | 12 | 7 |
| 0.70 | 0 | 2 | 12 | 8 | 11 | 3 | 9 | 12 | 12 | 5 |
| 0.75 | 0 | 2 | 11 | 8 | 11 | 2 | 8 | 10 | 12 | 4 |
| 0.80 | 0 | 1 | 9 | 5 | 11 | 2 | 5 | 8 | 12 | 2 |
| 0.85 | 0 | 1 | 9 | 5 | 9 | 1 | 5 | 6 | 12 | 1 |
| 0.90 | 0 | 1 | 9 | 5 | 7 | 1 | 3 | 4 | 12 | 0 |
| 0.95 | 0 | 1 | 6 | 3 | 7 | 1 | 2 | 2 | 12 | 0 |
| 0.96 | 0 | 1 | 6 | 2 | 7 | 1 | 2 | 2 | 11 | 0 |
| 0.97 | 0 | 0 | 6 | 2 | 6 | 1 | 2 | 1 | 11 | 0 |
| 0.98 | 0 | 0 | 5 | 1 | 5 | 1 | 2 | 1 | 11 | 0 |
| 0.99 | 0 | 0 | 2 | 0 | 2 | 0 | 0 | 1 | 6 | 0 |
