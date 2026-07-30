# ai-models/profiling

Construction of the per user reference profile: the feature means, the per feature standard
deviations used to standardise a new session, and the calibration parameters that map a scaled
Manhattan distance to the 0 to 100 risk value.

No heavyweight model artefacts are expected here, since the detector is distance based rather than a
trained classifier. A profile is a small set of summary statistics per user, which is also why it fits
in a single DynamoDB item (`PROFILE#<user>`, see [../../database/schema.md](../../database/schema.md)).

Files expected here: the profile building script, the calibration script, and the serialised profile
statistics written to `data/processed/`.

Number of enrolment repetitions per profile and the calibration mapping: TODO.

Owner: Arushi Tiwari (23BIT0181).
