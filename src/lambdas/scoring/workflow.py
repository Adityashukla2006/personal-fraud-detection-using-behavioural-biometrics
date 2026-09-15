"""Starts the response workflow for a confirmed transfer.

Asynchronous by design: the execution is started and scoring returns at once, without waiting on a
step-up that may take minutes (architecture section 5.1). The decision id is the execution name, so
a retried request cannot start the same transfer twice.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from fraudcore.policy import Decision, Response
from scoring.request import CheckpointRequest


class Workflow:
    def __init__(
        self,
        client: Any,
        state_machine_arn: str,
        step_up_seconds: int,
        review_seconds: int,
    ) -> None:
        self._client = client
        self._arn = state_machine_arn
        self._step_up_seconds = step_up_seconds
        self._review_seconds = review_seconds

    def start(
        self,
        uid: str,
        transfer_id: str,
        request: CheckpointRequest,
        decision: Decision,
        response: Response,
    ) -> None:
        if request.transaction is None:
            raise ValueError("a transfer workflow needs a transaction")
        payload = {
            "transfer_id": transfer_id,
            "decision_id": transfer_id,
            "uid": uid,
            "amount": request.transaction.amount,
            "payee_id": request.transaction.payee_id,
            "action": decision.action,
            "response": response,
            "risk": decision.risk,
            "hold_seconds": self._step_up_seconds,
            "review_seconds": self._review_seconds,
        }
        try:
            self._client.start_execution(
                stateMachineArn=self._arn, name=transfer_id, input=json.dumps(payload)
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise
