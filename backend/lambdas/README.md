# backend/lambdas

The two AWS Lambda functions in the deployment.

- `score/`, invoked synchronously by API Gateway on every submitted session, returning a risk value,
  a response tier and a feature level explanation.
- `updater/`, invoked on an EventBridge Scheduler schedule, applying the guarded update policy to
  candidate profile updates.

Both import the shared feature and scoring module from `../shared/`. Both are Python 3.12.

Owner: Aditya Shukla (23BIT0250).
