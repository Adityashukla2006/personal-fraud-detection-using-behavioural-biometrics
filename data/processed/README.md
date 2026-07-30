# data/processed

Derived artefacts produced from `../raw/` by the scripts in `ai-models/`.

Files expected here:

- The 12 feature matrix per subject, extracted from the benchmark repetitions.
- Per user reference profiles: feature means, standard deviations and calibration parameters.
- Genuine and impostor score distributions used for the EER, FAR and FRR calculation.
- Generated adversarial sessions (bot typing, replay, impostor) and drift injected genuine sessions.

Contents are not committed. Everything here must be reproducible from `../raw/`, so nothing should be
edited by hand.

Owner: Arushi Tiwari (23BIT0181).
