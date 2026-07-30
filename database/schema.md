# DynamoDB Schema

Single-table design. All items live in one table, distinguished by the prefix on the partition key.

- Partition key: `PK` (string)
- Sort key: `SK` (string)
- TTL attribute: `expiresAt` (number, epoch seconds), applied to session items only
- Billing mode: on demand, so there is no provisioned capacity and no idle cost

Table name: TODO.

## Item types

### 1. `PROFILE#<user>`

The per user reference profile read on every scoring request and rewritten by the guarded updater.

| Attribute | Type | Description |
| --- | --- | --- |
| `PK` | S | `PROFILE#<user>` |
| `SK` | S | `PROFILE` |
| `means` | L | Mean of each of the 12 features |
| `stdevs` | L | Standard deviation of each of the 12 features, used to standardise a new session |
| `calibration` | M | Parameters mapping scaled Manhattan distance to the 0 to 100 risk value |
| `enrolCount` | N | Number of sessions contributing to the profile |
| `version` | N | Incremented on every accepted update, so profile displacement can be traced |
| `updatedAt` | N | Epoch seconds of the last accepted update |

Access patterns:

- Read one profile by user, on every scoring request (`GetItem` on `PK = PROFILE#<user>`, `SK = PROFILE`).
- Conditional write from the updater Lambda, guarded on `version` so concurrent updates cannot
  silently overwrite each other.

### 2. `SESSION#<id>`

A submitted session: the extracted feature vector and the context needed by the guards. Raw timing
events are not retained beyond feature extraction, and typed content is never received at all.

| Attribute | Type | Description |
| --- | --- | --- |
| `PK` | S | `SESSION#<id>` |
| `SK` | S | `USER#<user>#<timestamp>` |
| `features` | L | The 12 extracted timing features |
| `screen` | S | `login` or `transfer` |
| `deviceHash` | S | Device fingerprint hash, consumed by the device anomaly guard |
| `authContext` | M | Credential and authentication signals consumed by the guards |
| `expiresAt` | N | TTL, session items are expired automatically |

Access patterns:

- Write one item per submitted session, from the score Lambda.
- Query a user's recent sessions, from the updater Lambda, to assess sustained low risk.

TTL duration: TODO.

### 3. `DECISION#<id>`

The scored outcome and its explanation, retained for the demonstration and for the latency and
attack detection measurements.

| Attribute | Type | Description |
| --- | --- | --- |
| `PK` | S | `DECISION#<id>` |
| `SK` | S | `USER#<user>#<timestamp>` |
| `risk` | N | Calibrated risk value, 0 to 100 |
| `tier` | S | `monitor`, `step-up`, `hold` or `block` |
| `distance` | N | Raw scaled Manhattan distance before calibration |
| `topDeviations` | L | The three highest deviating features, each with direction and magnitude |
| `latencyMs` | N | Server side scoring latency, aggregated into the p50 and p95 figures |
| `coldStart` | BOOL | Whether the invocation was a cold Lambda start |

Access patterns:

- Write one item per scoring request.
- Query a user's recent decisions, from the updater Lambda, to evaluate the sustained low risk guard.
- Scan or export for the latency, cost and per attack class detection tables in `results/tables/`.
