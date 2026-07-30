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

Outputs are written to `results/figures/` and `results/tables/`. All numeric results: TODO.

Owner: Arushi Tiwari (23BIT0181).
