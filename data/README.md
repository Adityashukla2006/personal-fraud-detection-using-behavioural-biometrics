# data

Dataset workspace for the offline track.

- `raw/`, the CMU keystroke dynamics benchmark exactly as downloaded.
- `processed/`, derived artefacts: extracted feature matrices, per user reference profiles, and the
  synthetic adversarial and drifted session sets used by the experiments.

Neither folder's contents are committed. Both are excluded in `.gitignore`, with the README files
negated so the folder structure stays visible on GitHub. Every processed artefact must be
regenerable from `raw/` by the scripts in `ai-models/`.

Owner: Arushi Tiwari (23BIT0181).
