### B4 sweep: `sentence-transformers/all-MiniLM-L6-v2`

QQP: 2,500 duplicates, 2,500 not; adversarial: 191 near-misses, 49 hard positives.

**No threshold from 0.80 to 0.99 keeps false hits at or below 2% on both sets: at most 50 of the 2,500 QQP non-duplicates, and 3 of the 191 near-misses.** The breakdown is at 0.99, the sweep's highest.

| Threshold | QQP false hits | QQP recall | Near-miss false hits | Hard-positive recall |
|---|---|---|---|---|
| 0.80 | 19.16% (479/2,500) | 75.3% (1,882/2,500) | 65.45% (125/191) | 49.0% (24/49) |
| 0.81 | 17.44% (436/2,500) | 72.8% (1,819/2,500) | 59.69% (114/191) | 49.0% (24/49) |
| 0.82 | 16.20% (405/2,500) | 69.9% (1,747/2,500) | 59.16% (113/191) | 44.9% (22/49) |
| 0.83 | 14.52% (363/2,500) | 67.5% (1,688/2,500) | 56.02% (107/191) | 40.8% (20/49) |
| 0.84 | 13.20% (330/2,500) | 64.0% (1,599/2,500) | 52.88% (101/191) | 36.7% (18/49) |
| 0.85 | 11.96% (299/2,500) | 60.4% (1,510/2,500) | 49.21% (94/191) | 30.6% (15/49) |
| 0.86 | 10.60% (265/2,500) | 57.4% (1,435/2,500) | 46.60% (89/191) | 22.4% (11/49) |
| 0.87 | 9.28% (232/2,500) | 53.8% (1,344/2,500) | 42.41% (81/191) | 16.3% (8/49) |
| 0.88 | 8.24% (206/2,500) | 49.5% (1,237/2,500) | 38.74% (74/191) | 12.2% (6/49) |
| 0.89 | 7.08% (177/2,500) | 45.6% (1,140/2,500) | 35.60% (68/191) | 10.2% (5/49) |
| 0.90 | 5.76% (144/2,500) | 41.4% (1,036/2,500) | 30.37% (58/191) | 8.2% (4/49) |
| 0.91 | 4.92% (123/2,500) | 37.6% (941/2,500) | 27.75% (53/191) | 2.0% (1/49) |
| 0.92 | 4.32% (108/2,500) | 33.3% (832/2,500) | 24.61% (47/191) | 2.0% (1/49) |
| 0.93 | 3.20% (80/2,500) | 29.4% (734/2,500) | 20.94% (40/191) | 2.0% (1/49) |
| 0.94 | 2.32% (58/2,500) | 25.2% (629/2,500) | 17.28% (33/191) | 2.0% (1/49) |
| 0.95 | 1.72% (43/2,500) | 21.2% (530/2,500) | 15.71% (30/191) | 2.0% (1/49) |
| 0.96 | 1.16% (29/2,500) | 16.7% (418/2,500) | 13.09% (25/191) | 0.0% (0/49) |
| 0.97 | 0.80% (20/2,500) | 12.5% (313/2,500) | 10.47% (20/191) | 0.0% (0/49) |
| 0.98 | 0.40% (10/2,500) | 8.2% (205/2,500) | 9.42% (18/191) | 0.0% (0/49) |
| 0.99 | 0.12% (3/2,500) | 4.5% (112/2,500) | 6.81% (13/191) | 0.0% (0/49) |

False hits at 0.99, by category (pairs that should not hit):

| Set | Category | False hits |
|---|---|---|
| QQP | non-duplicates | 0.12% (3/2,500) |
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
| QQP | duplicates | 4.5% (112/2,500) |
| Adversarial | paraphrase | 0.0% (0/49) |

The near-misses that hit at 0.99, 13 of 191:

| Pair | Category | Similarity | Question | Near-miss |
|---|---|---|---|---|
| adv-171 | reversal | 0.998 | What's the flight time from New York to London? | What's the flight time from London to New York? |
| adv-176 | reversal | 0.998 | Is a crocodile bigger than an alligator? | Is an alligator bigger than a crocodile? |
| adv-174 | reversal | 0.997 | How do I cast a float to an int in C? | How do I cast an int to a float in C? |
| adv-164 | reversal | 0.996 | How do I convert a string to an integer in Python? | How do I convert an integer to a string in Python? |
| adv-169 | reversal | 0.996 | How do I transfer money from PayPal to my bank account? | How do I transfer money from my bank account to PayPal? |
| adv-173 | reversal | 0.996 | How do I convert a PNG to a JPG? | How do I convert a JPG to a PNG? |
| adv-168 | reversal | 0.996 | What is the exchange rate from USD to EUR? | What is the exchange rate from EUR to USD? |
| adv-175 | reversal | 0.995 | How do I convert Celsius to Fahrenheit? | How do I convert Fahrenheit to Celsius? |
| adv-88 | unit | 0.995 | What is 30 degrees Celsius in Fahrenheit? | What is 30 degrees Fahrenheit in Celsius? |
| adv-165 | reversal | 0.995 | How do I convert JSON to YAML? | How do I convert YAML to JSON? |
| adv-167 | reversal | 0.993 | Is Python faster than Java? | Is Java faster than Python? |
| adv-170 | reversal | 0.991 | How do I migrate from MySQL to PostgreSQL? | How do I migrate from PostgreSQL to MySQL? |
| adv-172 | reversal | 0.990 | How do I switch from Android to iPhone? | How do I switch from iPhone to Android? |

Near-misses at or above each threshold, by category (each category's pairs in brackets):

| Threshold | number (22) | entity (22) | action (22) | negation (18) | unit (18) | year (18) | comparison (20) | technical (21) | reversal (14) | scope (16) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.80 | 17 | 8 | 12 | 16 | 9 | 14 | 17 | 10 | 12 | 10 |
| 0.81 | 14 | 6 | 11 | 16 | 9 | 14 | 15 | 9 | 12 | 8 |
| 0.82 | 14 | 6 | 10 | 16 | 9 | 14 | 15 | 9 | 12 | 8 |
| 0.83 | 13 | 6 | 9 | 16 | 9 | 12 | 15 | 8 | 12 | 7 |
| 0.84 | 12 | 5 | 8 | 16 | 8 | 11 | 15 | 7 | 12 | 7 |
| 0.85 | 10 | 3 | 8 | 16 | 8 | 10 | 15 | 5 | 12 | 7 |
| 0.86 | 9 | 3 | 8 | 16 | 8 | 9 | 13 | 4 | 12 | 7 |
| 0.87 | 7 | 2 | 6 | 15 | 8 | 7 | 13 | 4 | 12 | 7 |
| 0.88 | 6 | 2 | 5 | 15 | 7 | 7 | 12 | 2 | 12 | 6 |
| 0.89 | 5 | 2 | 3 | 15 | 6 | 6 | 12 | 1 | 12 | 6 |
| 0.90 | 2 | 1 | 3 | 14 | 6 | 6 | 9 | 1 | 12 | 4 |
| 0.91 | 2 | 1 | 2 | 13 | 5 | 4 | 9 | 1 | 12 | 4 |
| 0.92 | 2 | 1 | 2 | 11 | 5 | 2 | 8 | 1 | 12 | 3 |
| 0.93 | 2 | 0 | 2 | 9 | 3 | 2 | 6 | 1 | 12 | 3 |
| 0.94 | 2 | 0 | 1 | 9 | 2 | 1 | 4 | 1 | 12 | 1 |
| 0.95 | 2 | 0 | 1 | 8 | 2 | 1 | 2 | 1 | 12 | 1 |
| 0.96 | 2 | 0 | 0 | 7 | 2 | 0 | 1 | 0 | 12 | 1 |
| 0.97 | 0 | 0 | 0 | 5 | 2 | 0 | 1 | 0 | 12 | 0 |
| 0.98 | 0 | 0 | 0 | 3 | 2 | 0 | 1 | 0 | 12 | 0 |
| 0.99 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 12 | 0 |
