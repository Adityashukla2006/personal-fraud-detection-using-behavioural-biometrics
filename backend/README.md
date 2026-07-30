# backend

The serverless application: the AWS Lambda functions behind the API Gateway HTTP API, plus the one
module they share with the offline analysis track.

Contents:

- `lambdas/score/`, the per session scoring function (components C2 and C3).
- `lambdas/updater/`, the scheduled guarded profile updater (component C6).
- `shared/`, the feature specification and scoring module imported by both Lambdas and by the
  offline notebooks.

Runtime is Python 3.12. Deployment is by console upload or zip, with no provisioned infrastructure
and no idle cost, per objective O2.

Owner: Aditya Shukla (23BIT0250), with `shared/` jointly owned (see
[../docs/work-distribution.md](../docs/work-distribution.md)).
