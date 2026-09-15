"""Writes every bfd event to the S3 audit lake, one object per event.

EventBridge cannot target S3 directly, and Firehose is excluded (architecture section 11.2), so this
function is the bridge. The key comes from the event itself (``fraudcore.events.lake_key``), so a
redelivered event rewrites its own object rather than adding a duplicate. The bucket's default
encryption applies the lake key; nothing here chooses it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from fraudcore.events import lake_key

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)


def handle(event: Mapping[str, Any], s3: Any, bucket: str) -> dict[str, str]:
    key = lake_key(event)
    body = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    s3.put_object(
        Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="application/json"
    )
    LOGGER.info("archived %s", key)
    return {"key": key}


_s3: Any = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, str]:
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return handle(event, _s3, os.environ["LAKE_BUCKET"])
