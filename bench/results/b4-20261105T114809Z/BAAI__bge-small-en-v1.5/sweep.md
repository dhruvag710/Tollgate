### B4 sweep: `BAAI/bge-small-en-v1.5`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

**No threshold from 0.80 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.80 | 36.04% (901/2,500) | 94.2% (2,355/2,500) | 84.82% (162/191) | 89.8% (44/49) |
| 0.81 | 33.24% (831/2,500) | 92.1% (2,302/2,500) | 81.15% (155/191) | 81.6% (40/49) |
| 0.82 | 30.48% (762/2,500) | 89.4% (2,236/2,500) | 75.92% (145/191) | 77.6% (38/49) |
| 0.83 | 27.56% (689/2,500) | 86.3% (2,158/2,500) | 72.77% (139/191) | 73.5% (36/49) |
| 0.84 | 24.88% (622/2,500) | 83.2% (2,081/2,500) | 68.06% (130/191) | 65.3% (32/49) |
| 0.85 | 22.04% (551/2,500) | 79.3% (1,982/2,500) | 64.92% (124/191) | 55.1% (27/49) |
| 0.86 | 19.12% (478/2,500) | 76.0% (1,901/2,500) | 60.73% (116/191) | 42.9% (21/49) |
| 0.87 | 16.28% (407/2,500) | 71.7% (1,793/2,500) | 56.54% (108/191) | 40.8% (20/49) |
| 0.88 | 13.76% (344/2,500) | 67.6% (1,689/2,500) | 52.88% (101/191) | 30.6% (15/49) |
| 0.89 | 11.52% (288/2,500) | 62.0% (1,549/2,500) | 46.60% (89/191) | 28.6% (14/49) |
| 0.90 | 9.36% (234/2,500) | 56.8% (1,421/2,500) | 41.88% (80/191) | 24.5% (12/49) |
| 0.91 | 7.64% (191/2,500) | 51.4% (1,285/2,500) | 36.65% (70/191) | 18.4% (9/49) |
| 0.92 | 6.16% (154/2,500) | 46.2% (1,155/2,500) | 29.84% (57/191) | 12.2% (6/49) |
| 0.93 | 4.96% (124/2,500) | 40.2% (1,006/2,500) | 25.13% (48/191) | 8.2% (4/49) |
| 0.94 | 3.80% (95/2,500) | 34.5% (862/2,500) | 18.85% (36/191) | 6.1% (3/49) |
| 0.95 | 2.88% (72/2,500) | 28.8% (720/2,500) | 10.47% (20/191) | 2.0% (1/49) |
| 0.96 | 1.84% (46/2,500) | 23.2% (580/2,500) | 8.90% (17/191) | 0.0% (0/49) |
| 0.97 | 1.00% (25/2,500) | 18.0% (450/2,500) | 7.85% (15/191) | 0.0% (0/49) |
| 0.98 | 0.40% (10/2,500) | 12.4% (311/2,500) | 6.81% (13/191) | 0.0% (0/49) |
| 0.99 | 0.28% (7/2,500) | 6.4% (160/2,500) | 6.81% (13/191) | 0.0% (0/49) |

False hits at 0.99, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 0.28% (7/2,500) |
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
| QQP | duplicates | 6.4% (160/2,500) |
| Adversarial | paraphrase | 0.0% (0/49) |

The near-misses that hit at 0.99, 13 of 191:

| Pair | Category | Similarity | Question | Near-miss |
|---|---|---|---|---|
| adv-174 | reversal | 0.999 | How do I cast a float to an int in C? | How do I cast an int to a float in C? |
| adv-169 | reversal | 0.998 | How do I transfer money from PayPal to my bank account? | How do I transfer money from my bank account to PayPal? |
| adv-176 | reversal | 0.997 | Is a crocodile bigger than an alligator? | Is an alligator bigger than a crocodile? |
| adv-168 | reversal | 0.997 | What is the exchange rate from USD to EUR? | What is the exchange rate from EUR to USD? |
| adv-171 | reversal | 0.997 | What's the flight time from New York to London? | What's the flight time from London to New York? |
| adv-175 | reversal | 0.997 | How do I convert Celsius to Fahrenheit? | How do I convert Fahrenheit to Celsius? |
| adv-172 | reversal | 0.996 | How do I switch from Android to iPhone? | How do I switch from iPhone to Android? |
| adv-170 | reversal | 0.996 | How do I migrate from MySQL to PostgreSQL? | How do I migrate from PostgreSQL to MySQL? |
| adv-88 | unit | 0.995 | What is 30 degrees Celsius in Fahrenheit? | What is 30 degrees Fahrenheit in Celsius? |
| adv-167 | reversal | 0.995 | Is Python faster than Java? | Is Java faster than Python? |
| adv-173 | reversal | 0.995 | How do I convert a PNG to a JPG? | How do I convert a JPG to a PNG? |
| adv-165 | reversal | 0.994 | How do I convert JSON to YAML? | How do I convert YAML to JSON? |
| adv-164 | reversal | 0.991 | How do I convert a string to an integer in Python? | How do I convert an integer to a string in Python? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.80 | 20 | 13 | 21 | 18 | 14 | 12 | 18 | 20 | 13 | 13 |
| 0.81 | 19 | 11 | 20 | 18 | 13 | 11 | 17 | 20 | 13 | 13 |
| 0.82 | 18 | 8 | 19 | 17 | 12 | 9 | 17 | 20 | 12 | 13 |
| 0.83 | 18 | 7 | 18 | 16 | 12 | 8 | 15 | 20 | 12 | 13 |
| 0.84 | 16 | 7 | 17 | 16 | 11 | 8 | 13 | 19 | 12 | 11 |
| 0.85 | 14 | 7 | 16 | 16 | 11 | 8 | 10 | 19 | 12 | 11 |
| 0.86 | 14 | 6 | 14 | 15 | 11 | 7 | 9 | 19 | 12 | 9 |
| 0.87 | 12 | 5 | 14 | 15 | 10 | 7 | 9 | 17 | 12 | 7 |
| 0.88 | 12 | 4 | 14 | 11 | 10 | 7 | 9 | 15 | 12 | 7 |
| 0.89 | 10 | 3 | 14 | 11 | 9 | 3 | 7 | 14 | 12 | 6 |
| 0.90 | 8 | 2 | 13 | 10 | 9 | 3 | 5 | 13 | 12 | 5 |
| 0.91 | 6 | 1 | 10 | 10 | 8 | 2 | 5 | 11 | 12 | 5 |
| 0.92 | 3 | 1 | 10 | 8 | 5 | 2 | 5 | 7 | 12 | 4 |
| 0.93 | 2 | 0 | 8 | 6 | 4 | 2 | 5 | 6 | 12 | 3 |
| 0.94 | 2 | 0 | 3 | 4 | 2 | 2 | 3 | 6 | 12 | 2 |
| 0.95 | 1 | 0 | 1 | 0 | 2 | 2 | 1 | 1 | 12 | 0 |
| 0.96 | 1 | 0 | 1 | 0 | 2 | 1 | 0 | 0 | 12 | 0 |
| 0.97 | 1 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 12 | 0 |
| 0.98 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 12 | 0 |
| 0.99 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 12 | 0 |
