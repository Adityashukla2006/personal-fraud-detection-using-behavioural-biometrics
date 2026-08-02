# ai-models/experiments

Evaluation and the poisoning and guarded update experiments, which together are the research
contribution of the project (objectives O3 and O5).

Experiments expected here:

- Detection baseline: EER, FAR, FRR and the ROC curve over the 51 CMU subjects, compared against the
  published baseline for the dataset.
- Naive adaptive update loop, then the profile poisoning attack against it: sessions to successful
  impersonation, and profile displacement over time.
- The guarded update policy, swept over the guard threshold, producing the poisoning resistance
  against false rejection tradeoff curve.
- Genuine drift injection, measuring false rejection rate with and without adaptation.

Outputs are written to `results/figures/` and `results/tables/`.

## Detection baseline (O3) — complete

`baseline_eer.py`, feature set A (the 31 raw timing columns), published protocol, 51 subjects.

| Dispersion | Mean EER | Std | Min / Max |
| --- | --- | --- | --- |
| Mean absolute deviation | **0.0962** | 0.0687 | 0.0100 / 0.3250 |
| Standard deviation | 0.0930 | 0.0697 | 0.0000 / 0.3050 |

The MAD figure reproduces the published baseline of 0.0962 for the scaled Manhattan detector
exactly, which is the comparison objective O3 asks for. Reaching that number to four decimal places
is also the strongest available evidence that the feature handling, the split protocol and the
distance are jointly correct.

Standard deviation scores marginally lower, by 0.0032 EER. That is not a reason to adopt it: the
value of the MAD figure is that it is comparable to published work, and a slightly better number
that matches nothing is worth less than an identical one that matches the literature. Recorded here
so the choice rests on measurement, see issue #1.

Mean false acceptance at fixed false rejection targets, MAD:

| FRR target | 1% | 5% | 10% | 20% |
| --- | --- | --- | --- | --- |
| Mean FAR | 0.368 | 0.179 | 0.104 | 0.065 |

This table is the empirical case for RG4. A single accept/reject threshold forces a choice between
rejecting one genuine user in a hundred while admitting 37% of impostors, or holding impostors to
6.5% while rejecting one genuine user in five. Neither is deployable, which is what the four graded
response tiers in O4 exist to avoid.

Per-subject values: [`results/tables/baseline_eer.csv`](../../results/tables/baseline_eer.csv).

Remaining numeric results: TODO, pending the poisoning, guarded update and drift experiments.

Owner: Arushi Tiwari (23BIT0181).
