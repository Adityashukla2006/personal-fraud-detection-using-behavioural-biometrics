# backend/shared

The feature specification and scoring module: the 12 feature definitions, the standardisation step
and the scaled Manhattan distance.

This one module is imported by both the offline analysis in `ai-models/` and by the score Lambda in
`lambdas/score/`. That is deliberate and it is what guarantees the deployed scorer and the evaluated
scorer are identical, so the error rates reported from the offline track describe the behaviour of
the system actually running on AWS. Any change to a feature definition or to the distance must be
made here and nowhere else.

Files expected here: `features.py` (the 12 feature definitions and their extraction from raw timing
events), `scoring.py` (standardisation and scaled Manhattan distance), and `__init__.py`.

Jointly owned by Aditya Shukla (23BIT0250) and Arushi Tiwari (23BIT0181).
