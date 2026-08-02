# data/raw

The CMU keystroke dynamics benchmark, DSL-StrongPasswordData: 51 subjects, one fixed password, 400
repetitions each across 8 sessions.

**The dataset is NOT committed to this repository.** It must be downloaded separately and placed in
this folder before any script in `ai-models/` will run. To fetch and verify it, run from the
repository root:

```
python ai-models/download_dataset.py
```

- Expected filename: `DSL-StrongPasswordData.csv`
- Citation: Killourhy, K. S. and Maxion, R. A. (2009). Comparing Anomaly-Detection Algorithms for
  Keystroke Dynamics. DSN 2009.
- Download URL: https://www.cs.cmu.edu/~keystroke/DSL-StrongPasswordData.csv
- Size: 4,669,935 bytes
- SHA-256: `b11d23538b1865fa6ecf4e8b78567caa312e9c1027604bb022fcc6ad7eaa7a33`

**Do not download this file by hand.** A partial download still parses as valid CSV and still looks
like a plausible dataset, it simply contains fewer subjects, so a truncated copy would corrupt every
error rate downstream without raising an error anywhere. This was not hypothetical: the first
retrieval during setup silently returned 36 subjects instead of 51. `download_dataset.py` verifies
size, SHA-256 and parsed shape, and refuses to proceed on any mismatch.

## Contents as verified

| Property | Value |
| --- | --- |
| Rows | 20,400 (51 subjects x 8 sessions x 50 repetitions) |
| Columns | 34: `subject`, `sessionIndex`, `rep`, and 31 timing columns |
| Timing columns | 11 `H.*` hold, 10 `DD.*` down-down, 10 `UD.*` up-down |
| Missing values | 0 |

Note that 22,118 of the `UD.*` values are negative. This is normal finger overlap, where the next
key goes down before the previous one comes up, and those values must not be clipped to zero.

Nothing in this folder is modified in place. Derived artefacts go to `../processed/`.

Owner: Arushi Tiwari (23BIT0181).
