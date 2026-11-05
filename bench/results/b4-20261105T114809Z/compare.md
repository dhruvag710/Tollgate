### B4: embedding models compared

Embed: 500 single queries per model, one PyTorch thread, after 20 to warm up; the miss path's share before the Postgres lookup (B5 times that). Memory: resident memory added by loading the model. Rates are at each model's chosen threshold, with the counts they come from.

| Model | Threshold chosen | QQP recall | Hard-positive recall | QQP false hits | Near-miss false hits | Embed p50 / p99 (ms) | Memory (MB) | Load (s) |
|---|---|---|---|---|---|---|---|---|
| BAAI/bge-small-en-v1.5 | none meets 2% | n/a | n/a | n/a | n/a | 19.7 / 37.2 | 148 | 0.4 |
| sentence-transformers/all-MiniLM-L6-v2 | none meets 2% | n/a | n/a | n/a | n/a | 10.1 / 18.5 | 109 | 0.3 |
| intfloat/e5-small-v2 | none meets 2% | n/a | n/a | n/a | n/a | 20.7 / 70.8 | 147 | 0.4 |
| thenlper/gte-small | none meets 2% | n/a | n/a | n/a | n/a | 19.2 / 33.5 | 147 | 0.4 |

No model keeps false hits at or below 2% on both sets at any threshold from 0.80 to 0.99.

False hits and recall by category, at each model's chosen threshold:

| Pairs | bge-small-en-v1.5 (none chosen; at 0.99) | all-MiniLM-L6-v2 (none chosen; at 0.99) | e5-small-v2 (none chosen; at 0.99) | gte-small (none chosen; at 0.99) |
|---|---|---|---|---|
| False hits: QQP non-duplicates | 0.28% (7/2,500) | 0.12% (3/2,500) | 0.64% (16/2,500) | 0.52% (13/2,500) |
| False hits: number | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) |
| False hits: entity | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) |
| False hits: action | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) | 0.00% (0/22) |
| False hits: negation | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) |
| False hits: unit | 5.56% (1/18) | 5.56% (1/18) | 11.11% (2/18) | 5.56% (1/18) |
| False hits: year | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) | 0.00% (0/18) |
| False hits: comparison | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) | 0.00% (0/20) |
| False hits: technical | 0.00% (0/21) | 0.00% (0/21) | 0.00% (0/21) | 0.00% (0/21) |
| False hits: reversal | 85.71% (12/14) | 85.71% (12/14) | 85.71% (12/14) | 85.71% (12/14) |
| False hits: scope | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) | 0.00% (0/16) |
| False hits: all near-misses | 6.81% (13/191) | 6.81% (13/191) | 7.33% (14/191) | 6.81% (13/191) |
| Recall: QQP duplicates | 6.4% (160/2,500) | 4.5% (112/2,500) | 12.0% (300/2,500) | 12.4% (309/2,500) |
| Recall: paraphrase | 0.0% (0/49) | 0.0% (0/49) | 0.0% (0/49) | 0.0% (0/49) |

Timing conditions (whole machine, during each model's timed queries):

| Model | System CPU busy | Fastest core at its slowest (MHz) | Flags |
|---|---|---|---|
| BAAI/bge-small-en-v1.5 | 18.5 percent | 2,701 of 2,701 | none |
| sentence-transformers/all-MiniLM-L6-v2 | 14.6 percent | 2,701 of 2,701 | none |
| intfloat/e5-small-v2 | 15.3 percent | 48 of 2,701 | clock limited to 0 of 2701 MHz; fastest core fell to 48 of 2701 MHz |
| thenlper/gte-small | 14.3 percent | 2,701 of 2,701 | none |
