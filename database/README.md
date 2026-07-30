# database

The Amazon DynamoDB single-table design, component C4 of the architecture.

One table holds all three item types: reference profiles, submitted sessions under a TTL, and scored
decisions. A single table keeps the read path for scoring to one lookup, which matters for the
latency figures reported in objective O6, and keeps the deployment inside the always free tier.

- [schema.md](schema.md), item types, key schema, attributes and access patterns.

Files expected here in addition: the table creation parameters (partition key, sort key, TTL
attribute and billing mode) as a CLI snippet or console checklist.

Owner: Aditya Shukla (23BIT0250).
