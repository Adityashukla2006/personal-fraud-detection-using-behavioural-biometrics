"""The aggregator's one Athena query over the audit lake.

Released transfers, each joined to the behaviour score of the confirmation decision that started
it, so the batch layer can compare the typing on one payee's transfers with the same user's typing
on every other payee. Partition columns bound the scan to the lookback window, so the query reads
only those days' objects.
"""

from __future__ import annotations

import re
from datetime import datetime

_CATALOG_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def transfers_query(database: str, table: str, start: datetime, end: datetime) -> str:
    """Rows of ``uid, payee_id, amount, at, identity`` for transfers released from start to end.

    Catalog names are validated rather than escaped, and the dates are formatted here, so nothing
    from an event reaches the SQL text.
    """
    for name in (database, table):
        if not _CATALOG_NAME.fullmatch(name):
            raise ValueError(f"invalid catalog name {name!r}")
    if end < start:
        raise ValueError("end precedes start")

    first, last = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
    source = f'"{database}"."{table}"'
    return f"""WITH released AS (
  SELECT detail.uid AS uid, detail.payee_id AS payee_id, detail.amount AS amount,
         detail.decision_id AS decision_id,
         to_unixtime(from_iso8601_timestamp("time")) AS at
  FROM {source}
  WHERE event_type = 'transfer.completed' AND dt BETWEEN '{first}' AND '{last}'
    AND detail.status = 'released'
), identity AS (
  SELECT detail.decision_id AS decision_id, max(detail.scores.behaviour.score) AS score
  FROM {source}
  WHERE event_type = 'decision.scored' AND dt BETWEEN '{first}' AND '{last}'
    AND detail.checkpoint = 'confirmation'
  GROUP BY detail.decision_id
)
SELECT r.uid, r.payee_id, r.amount, r.at, i.score
FROM released r LEFT JOIN identity i ON i.decision_id = r.decision_id
ORDER BY r.uid, r.at"""
