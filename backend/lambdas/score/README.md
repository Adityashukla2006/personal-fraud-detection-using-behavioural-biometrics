# backend/lambdas/score

The scoring Lambda, component C3, invoked by API Gateway for every submitted session.

Per invocation it:

1. Extracts the 12 timing features from the submitted keydown and keyup timestamps.
2. Reads the caller's reference profile (`PROFILE#<user>`) from DynamoDB.
3. Standardises the feature vector against the profile means and standard deviations.
4. Computes the scaled Manhattan distance.
5. Maps the distance to a calibrated risk value from 0 to 100 and one of the four tiers
   (monitor, step-up, hold, block).
6. Returns the top 3 deviating features with direction and magnitude, and writes `SESSION#<id>` and
   `DECISION#<id>` items.

Files expected here: `handler.py`, `risk.py` (tier thresholds and calibration), and the packaging
manifest. Threshold and calibration constants: TODO, pending the offline evaluation in
`ai-models/experiments/`.

Owner: Aditya Shukla (23BIT0250).
