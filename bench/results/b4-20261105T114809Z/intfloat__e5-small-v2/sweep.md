### B4 sweep: `intfloat/e5-small-v2`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

**No threshold from 0.80 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.80 | 87.12% (2,178/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.81 | 84.80% (2,120/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.82 | 82.56% (2,064/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.83 | 79.88% (1,997/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.84 | 76.72% (1,918/2,500) | 100.0% (2,500/2,500) | 100.00% (191/191) | 100.0% (49/49) |
| 0.85 | 72.36% (1,809/2,500) | 100.0% (2,500/2,500) | 98.43% (188/191) | 100.0% (49/49) |
| 0.86 | 66.64% (1,666/2,500) | 99.9% (2,498/2,500) | 98.43% (188/191) | 100.0% (49/49) |
| 0.87 | 61.12% (1,528/2,500) | 99.8% (2,495/2,500) | 98.43% (188/191) | 100.0% (49/49) |
| 0.88 | 55.64% (1,391/2,500) | 99.3% (2,482/2,500) | 96.86% (185/191) | 95.9% (47/49) |
| 0.89 | 50.40% (1,260/2,500) | 98.6% (2,466/2,500) | 95.29% (182/191) | 89.8% (44/49) |
| 0.90 | 44.20% (1,105/2,500) | 97.1% (2,427/2,500) | 93.19% (178/191) | 87.8% (43/49) |
| 0.91 | 38.52% (963/2,500) | 94.3% (2,358/2,500) | 87.43% (167/191) | 83.7% (41/49) |
| 0.92 | 32.88% (822/2,500) | 89.4% (2,235/2,500) | 80.63% (154/191) | 71.4% (35/49) |
| 0.93 | 26.20% (655/2,500) | 82.4% (2,059/2,500) | 74.87% (143/191) | 61.2% (30/49) |
| 0.94 | 19.76% (494/2,500) | 74.0% (1,850/2,500) | 67.02% (128/191) | 38.8% (19/49) |
| 0.95 | 14.08% (352/2,500) | 63.5% (1,587/2,500) | 55.50% (106/191) | 28.6% (14/49) |
| 0.96 | 8.84% (221/2,500) | 51.0% (1,276/2,500) | 43.98% (84/191) | 18.4% (9/49) |
| 0.97 | 4.96% (124/2,500) | 37.7% (943/2,500) | 30.89% (59/191) | 4.1% (2/49) |
| 0.98 | 2.52% (63/2,500) | 23.3% (582/2,500) | 14.66% (28/191) | 0.0% (0/49) |
| 0.99 | 0.64% (16/2,500) | 12.0% (300/2,500) | 7.33% (14/191) | 0.0% (0/49) |

False hits at 0.99, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 0.64% (16/2,500) |
| Adversarial | number | 0.00% (0/22) |
| Adversarial | entity | 0.00% (0/22) |
| Adversarial | action | 0.00% (0/22) |
| Adversarial | negation | 0.00% (0/18) |
| Adversarial | unit | 11.11% (2/18) |
| Adversarial | year | 0.00% (0/18) |
| Adversarial | comparison | 0.00% (0/20) |
| Adversarial | technical | 0.00% (0/21) |
| Adversarial | reversal | 85.71% (12/14) |
| Adversarial | scope | 0.00% (0/16) |
| Adversarial | all near-misses | 7.33% (14/191) |

Recall at 0.99, by category (pairs that should hit):

| Set | Category | Recall |
|---|---|---|
| QQP | duplicates | 12.0% (300/2,500) |
| Adversarial | paraphrase | 0.0% (0/49) |

The near-misses that hit at 0.99, 14 of 191:

| Pair | Category | Similarity | Question | Near-miss |
|---|---|---|---|---|
| adv-173 | reversal | 0.999 | How do I convert a PNG to a JPG? | How do I convert a JPG to a PNG? |
| adv-169 | reversal | 0.999 | How do I transfer money from PayPal to my bank account? | How do I transfer money from my bank account to PayPal? |
| adv-175 | reversal | 0.999 | How do I convert Celsius to Fahrenheit? | How do I convert Fahrenheit to Celsius? |
| adv-174 | reversal | 0.999 | How do I cast a float to an int in C? | How do I cast an int to a float in C? |
| adv-170 | reversal | 0.999 | How do I migrate from MySQL to PostgreSQL? | How do I migrate from PostgreSQL to MySQL? |
| adv-171 | reversal | 0.999 | What's the flight time from New York to London? | What's the flight time from London to New York? |
| adv-172 | reversal | 0.999 | How do I switch from Android to iPhone? | How do I switch from iPhone to Android? |
| adv-164 | reversal | 0.998 | How do I convert a string to an integer in Python? | How do I convert an integer to a string in Python? |
| adv-167 | reversal | 0.998 | Is Python faster than Java? | Is Java faster than Python? |
| adv-168 | reversal | 0.998 | What is the exchange rate from USD to EUR? | What is the exchange rate from EUR to USD? |
| adv-88 | unit | 0.998 | What is 30 degrees Celsius in Fahrenheit? | What is 30 degrees Fahrenheit in Celsius? |
| adv-165 | reversal | 0.998 | How do I convert JSON to YAML? | How do I convert YAML to JSON? |
| adv-176 | reversal | 0.998 | Is a crocodile bigger than an alligator? | Is an alligator bigger than a crocodile? |
| adv-93 | unit | 0.993 | What is 100 km/h in mph? | What is 100 mph in km/h? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.80 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.81 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.82 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.83 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.84 | 22 | 22 | 22 | 18 | 18 | 18 | 20 | 21 | 14 | 16 |
| 0.85 | 22 | 20 | 22 | 18 | 17 | 18 | 20 | 21 | 14 | 16 |
| 0.86 | 22 | 20 | 22 | 18 | 17 | 18 | 20 | 21 | 14 | 16 |
| 0.87 | 22 | 20 | 22 | 18 | 17 | 18 | 20 | 21 | 14 | 16 |
| 0.88 | 22 | 18 | 22 | 18 | 17 | 17 | 20 | 21 | 14 | 16 |
| 0.89 | 22 | 18 | 22 | 18 | 17 | 16 | 20 | 20 | 14 | 15 |
| 0.90 | 21 | 16 | 22 | 18 | 17 | 15 | 20 | 20 | 14 | 15 |
| 0.91 | 21 | 11 | 22 | 18 | 16 | 12 | 20 | 20 | 12 | 15 |
| 0.92 | 19 | 7 | 22 | 18 | 15 | 10 | 18 | 19 | 12 | 14 |
| 0.93 | 18 | 6 | 22 | 18 | 15 | 7 | 16 | 16 | 12 | 13 |
| 0.94 | 13 | 3 | 19 | 18 | 15 | 6 | 15 | 14 | 12 | 13 |
| 0.95 | 11 | 2 | 14 | 16 | 12 | 6 | 13 | 10 | 12 | 10 |
| 0.96 | 7 | 1 | 11 | 15 | 11 | 4 | 8 | 9 | 12 | 6 |
| 0.97 | 5 | 0 | 7 | 11 | 7 | 3 | 6 | 3 | 12 | 5 |
| 0.98 | 1 | 0 | 1 | 4 | 5 | 1 | 0 | 2 | 12 | 2 |
| 0.99 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 12 | 0 |
