# ai-models

Track 1 of the project, the offline experiment over the CMU keystroke benchmark. It produces the
research result: Equal Error Rate against the published baseline, and the poisoning resistance
curve.

Contents:

- `feature-extraction/`, the 12 keystroke timing features.
- `profiling/`, per user reference profile statistics.
- `experiments/`, evaluation and the poisoning and guarded update experiments.

The detector is distance based rather than a trained network, so this folder holds analysis code and
profile statistics, not model weights. Feature and scoring logic itself is not duplicated here: it is
imported from [../backend/shared/](../backend/shared/README.md).

Owner: Arushi Tiwari (23BIT0181).
