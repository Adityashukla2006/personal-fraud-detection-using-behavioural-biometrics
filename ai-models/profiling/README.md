# ai-models/profiling

Construction of the per user reference profile: the feature means, the per feature dispersion used
to standardise a new session, and the calibration parameters that map a scaled Manhattan distance to
the 0 to 100 risk value.

Dispersion is the **mean absolute deviation**, not the standard deviation this file previously
specified. The "scaled" in the scaled Manhattan detector refers to MAD, and using it reproduces the
published baseline exactly (0.0962), which is the comparison objective O3 requires. Standard
deviation was measured alongside it and scores 0.0930, marginally lower but no longer comparable to
any published figure. See issue #1; the corresponding change to `backend/shared/` and to the
`PROFILE#<user>` item in `database/schema.md` is pending agreement.

No heavyweight model artefacts are expected here, since the detector is distance based rather than a
trained classifier. A profile is a small set of summary statistics per user, which is also why it fits
in a single DynamoDB item (`PROFILE#<user>`, see [../../database/schema.md](../../database/schema.md)).

Files expected here: the profile building script, the calibration script, and the serialised profile
statistics written to `data/processed/`.

**Enrolment repetitions per profile: 200.** This follows the published protocol so the reported
error rates stay comparable, and it lands on a session boundary: the benchmark's eight sessions were
recorded on separate days, so repetitions 1 to 200 are sessions 1 to 4 and the genuine test set is
sessions 5 to 8. Enrolment is therefore chronologically prior to testing, and the reported false
rejection rate already contains real behavioural drift rather than concealing it.

Calibration mapping from distance to the 0 to 100 risk value: TODO, pending O4.

Owner: Arushi Tiwari (23BIT0181).
