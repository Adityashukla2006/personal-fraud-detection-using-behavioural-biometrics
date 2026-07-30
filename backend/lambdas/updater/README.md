# backend/lambdas/updater

The scheduled profile updater, component C6, invoked by Amazon EventBridge Scheduler.

It reads recent `SESSION#<id>` and `DECISION#<id>` items and enforces the guarded update policy:
a reference profile is updated only when the candidate sessions show sustained low risk and there
is no credential, device or authentication anomaly attached to them. A candidate that fails any
guard is discarded and a CloudWatch metric is emitted on every rejected update, which is the
observable signal of an attempted poisoning campaign.

Files expected here: `handler.py`, `guards.py` (the individual guard predicates), and the packaging
manifest. Guard threshold values: TODO, pending the tradeoff curve in `ai-models/experiments/`.

This function is the deployed counterpart of the primary research objective O5.

Owner: Aditya Shukla (23BIT0250), guard policy specified with Arushi Tiwari (23BIT0181).
