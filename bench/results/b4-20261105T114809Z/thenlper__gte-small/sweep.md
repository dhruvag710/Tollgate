### B4 sweep: `thenlper/gte-small`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

**No threshold from 0.80 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.80 | 81.08% (2,027/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.81 | 78.60% (1,965/2,500) | 100.0% (2,500/2,500) | 98.95% (189/191) | 100.0% (49/49) |
| 0.82 | 76.20% (1,905/2,500) | 100.0% (2,500/2,500) | 98.95% (189/191) | 100.0% (49/49) |
| 0.83 | 73.32% (1,833/2,500) | 100.0% (2,500/2,500) | 98.43% (188/191) | 100.0% (49/49) |
| 0.84 | 69.24% (1,731/2,500) | 99.8% (2,495/2,500) | 97.91% (187/191) | 100.0% (49/49) |
| 0.85 | 64.80% (1,620/2,500) | 99.6% (2,491/2,500) | 97.38% (186/191) | 100.0% (49/49) |
| 0.86 | 60.00% (1,500/2,500) | 99.4% (2,484/2,500) | 95.29% (182/191) | 98.0% (48/49) |
| 0.87 | 54.88% (1,372/2,500) | 98.8% (2,471/2,500) | 93.19% (178/191) | 95.9% (47/49) |
| 0.88 | 49.84% (1,246/2,500) | 97.6% (2,439/2,500) | 90.05% (172/191) | 89.8% (44/49) |
| 0.89 | 45.04% (1,126/2,500) | 96.0% (2,399/2,500) | 86.39% (165/191) | 89.8% (44/49) |
| 0.90 | 39.84% (996/2,500) | 93.3% (2,333/2,500) | 81.68% (156/191) | 79.6% (39/49) |
| 0.91 | 34.20% (855/2,500) | 89.0% (2,225/2,500) | 74.87% (143/191) | 65.3% (32/49) |
| 0.92 | 28.16% (704/2,500) | 82.5% (2,063/2,500) | 61.78% (118/191) | 53.1% (26/49) |
| 0.93 | 21.76% (544/2,500) | 74.8% (1,871/2,500) | 55.50% (106/191) | 42.9% (21/49) |
| 0.94 | 16.08% (402/2,500) | 65.6% (1,640/2,500) | 47.64% (91/191) | 30.6% (15/49) |
| 0.95 | 11.00% (275/2,500) | 55.8% (1,394/2,500) | 35.08% (67/191) | 20.4% (10/49) |
| 0.96 | 7.44% (186/2,500) | 45.1% (1,128/2,500) | 27.75% (53/191) | 8.2% (4/49) |
| 0.97 | 4.16% (104/2,500) | 34.1% (853/2,500) | 17.28% (33/191) | 8.2% (4/49) |
| 0.98 | 2.32% (58/2,500) | 23.0% (575/2,500) | 10.47% (20/191) | 2.0% (1/49) |
| 0.99 | 0.52% (13/2,500) | 12.4% (309/2,500) | 6.81% (13/191) | 0.0% (0/49) |

False hits at 0.99, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 0.52% (13/2,500) |
| Adversarial | number | 0.00% (0/22) |
| Adversarial | entity | 0.00% (0/22) |
| Adversarial | action | 0.00% (0/22) |
| Adversarial | negation | 0.00% (0/18) |
| Adversarial | unit | 5.56% (1/18) |
| Adversarial | year | 0.00% (0/18) |
| Adversarial | comparison | 0.00% (0/20) |
| Adversarial | technical | 0.00% (0/21) |
| Adversarial | reversal | 85.71% (12/14) |
| Adversarial | scope | 0.00% (0/16) |
| Adversarial | all near-misses | 6.81% (13/191) |

Recall at 0.99, by category (pairs that should hit):

| Set | Category | Recall |
|---|---|---|
| QQP | duplicates | 12.4% (309/2,500) |
| Adversarial | paraphrase | 0.0% (0/49) |

The near-misses that hit at 0.99, 13 of 191:

| Pair | Category | Similarity | Question | Near-miss |
|---|---|---|---|---|
| adv-174 | reversal | 0.999 | How do I cast a float to an int in C? | How do I cast an int to a float in C? |
| adv-169 | reversal | 0.999 | How do I transfer money from PayPal to my bank account? | How do I transfer money from my bank account to PayPal? |
| adv-176 | reversal | 0.999 | Is a crocodile bigger than an alligator? | Is an alligator bigger than a crocodile? |
| adv-167 | reversal | 0.998 | Is Python faster than Java? | Is Java faster than Python? |
| adv-168 | reversal | 0.998 | What is the exchange rate from USD to EUR? | What is the exchange rate from EUR to USD? |
| adv-172 | reversal | 0.998 | How do I switch from Android to iPhone? | How do I switch from iPhone to Android? |
| adv-171 | reversal | 0.998 | What's the flight time from New York to London? | What's the flight time from London to New York? |
| adv-175 | reversal | 0.997 | How do I convert Celsius to Fahrenheit? | How do I convert Fahrenheit to Celsius? |
| adv-170 | reversal | 0.997 | How do I migrate from MySQL to PostgreSQL? | How do I migrate from PostgreSQL to MySQL? |
| adv-173 | reversal | 0.997 | How do I convert a PNG to a JPG? | How do I convert a JPG to a PNG? |
| adv-88 | unit | 0.997 | What is 30 degrees Celsius in Fahrenheit? | What is 30 degrees Fahrenheit in Celsius? |
| adv-165 | reversal | 0.996 | How do I convert JSON to YAML? | How do I convert YAML to JSON? |
| adv-164 | reversal | 0.996 | How do I convert a string to an integer in Python? | How do I convert an integer to a string in Python? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.80 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.81 | 22 | 21 | 22 | 18 | 17 | 18 | 20 | 21 | 14 | 16 |
| 0.82 | 22 | 21 | 22 | 18 | 17 | 18 | 20 | 21 | 14 | 16 |
| 0.83 | 22 | 21 | 22 | 18 | 17 | 18 | 20 | 20 | 14 | 16 |
| 0.84 | 22 | 20 | 22 | 18 | 17 | 18 | 20 | 20 | 14 | 16 |
| 0.85 | 22 | 19 | 22 | 18 | 17 | 18 | 20 | 20 | 14 | 16 |
| 0.86 | 22 | 16 | 22 | 18 | 17 | 17 | 20 | 20 | 14 | 16 |
| 0.87 | 22 | 14 | 22 | 18 | 17 | 16 | 20 | 20 | 13 | 16 |
| 0.88 | 22 | 13 | 22 | 18 | 15 | 14 | 20 | 20 | 13 | 15 |
| 0.89 | 21 | 11 | 21 | 18 | 14 | 14 | 19 | 20 | 13 | 14 |
| 0.90 | 20 | 9 | 18 | 18 | 13 | 14 | 18 | 20 | 13 | 13 |
| 0.91 | 18 | 7 | 17 | 17 | 13 | 11 | 17 | 19 | 12 | 12 |
| 0.92 | 15 | 5 | 15 | 16 | 11 | 7 | 13 | 14 | 12 | 10 |
| 0.93 | 13 | 5 | 14 | 15 | 10 | 5 | 12 | 12 | 12 | 8 |
| 0.94 | 8 | 4 | 13 | 14 | 9 | 5 | 10 | 9 | 12 | 7 |
| 0.95 | 5 | 1 | 9 | 12 | 6 | 4 | 7 | 6 | 12 | 5 |
| 0.96 | 3 | 1 | 7 | 12 | 4 | 2 | 4 | 5 | 12 | 3 |
| 0.97 | 3 | 1 | 1 | 8 | 2 | 2 | 2 | 1 | 12 | 1 |
| 0.98 | 1 | 0 | 1 | 3 | 2 | 0 | 1 | 0 | 12 | 0 |
| 0.99 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 12 | 0 |
