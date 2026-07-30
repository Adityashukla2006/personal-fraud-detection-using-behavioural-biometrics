# ai-models/feature-extraction

The 12 keystroke timing feature representation defined in objective O3, applied to the CMU benchmark
records and to sessions captured by the deployed client.

The four feature groups are:

1. **Hold times**, how long each key is held down (keydown to keyup).
2. **Flight times**, the inter key intervals between successive keystrokes.
3. **Rhythm**, total entry duration across the credential string.
4. **Corrections**, the correction rate over the entry.

Exact per feature definitions live in [../../backend/shared/](../../backend/shared/README.md) so that
the offline and deployed paths cannot diverge. Files expected here: notebooks and scripts that apply
that module to `data/raw/` and write feature matrices to `data/processed/`.

Owner: Arushi Tiwari (23BIT0181).
