"""Phase 3 exit: the data plane exists, is encrypted as designed, and round-trips real items.

Asserts only on observable state: the item read back, the table and bucket descriptions, and IAM's
own evaluation of each function role against the key prefixes it should and should not reach.
"""

from __future__ import annotations

import time
from typing import Any

import boto3
import pytest

pytestmark = pytest.mark.integration

GSI_WAIT_SECONDS = 30


class TestTable:
    def test_an_item_round_trips_through_the_table_and_the_gsi(
        self, aws: boto3.Session, outputs: dict[str, Any], test_key: str
    ) -> None:
        client = aws.client("dynamodb")
        table = outputs["table_name"]
        item = {
            "PK": {"S": test_key},
            "SK": {"S": "META"},
            "GSI1PK": {"S": test_key},
            "GSI1SK": {"S": "TS#2026-01-01T00:00:00Z"},
            "ttl": {"N": str(int(time.time()) + 3600)},
            "payload": {"S": "round-trip"},
        }

        client.put_item(TableName=table, Item=item)
        try:
            read = client.get_item(
                TableName=table,
                Key={"PK": item["PK"], "SK": item["SK"]},
                ConsistentRead=True,
            )
            assert read["Item"] == item

            # GSIs are eventually consistent, so poll rather than assert once.
            deadline = time.monotonic() + GSI_WAIT_SECONDS
            found: list[dict[str, Any]] = []
            while not found and time.monotonic() < deadline:
                found = client.query(
                    TableName=table,
                    IndexName=outputs["gsi1_name"],
                    KeyConditionExpression="GSI1PK = :pk",
                    ExpressionAttributeValues={":pk": item["GSI1PK"]},
                )["Items"]
                if not found:
                    time.sleep(1)
            assert found == [item]
        finally:
            client.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})

        gone = client.get_item(
            TableName=table, Key={"PK": item["PK"], "SK": item["SK"]}, ConsistentRead=True
        )
        assert "Item" not in gone

    def test_the_table_is_encrypted_with_the_templates_key(
        self, aws: boto3.Session, outputs: dict[str, Any]
    ) -> None:
        sse = aws.client("dynamodb").describe_table(TableName=outputs["table_name"])["Table"][
            "SSEDescription"
        ]
        assert sse["Status"] == "ENABLED"
        assert sse["SSEType"] == "KMS"
        assert sse["KMSMasterKeyArn"] == outputs["templates_key_arn"]

    def test_ttl_is_enabled_on_the_ttl_attribute(
        self, aws: boto3.Session, outputs: dict[str, Any]
    ) -> None:
        ttl = aws.client("dynamodb").describe_time_to_live(TableName=outputs["table_name"])[
            "TimeToLiveDescription"
        ]
        assert ttl["TimeToLiveStatus"] in {"ENABLED", "ENABLING"}
        assert ttl["AttributeName"] == "ttl"


class TestBuckets:
    def test_the_lake_is_encrypted_with_its_own_key(
        self, aws: boto3.Session, outputs: dict[str, Any]
    ) -> None:
        rule = aws.client("s3").get_bucket_encryption(Bucket=outputs["lake_bucket"])[
            "ServerSideEncryptionConfiguration"
        ]["Rules"][0]
        default = rule["ApplyServerSideEncryptionByDefault"]
        assert default["SSEAlgorithm"] == "aws:kms"
        assert default["KMSMasterKeyID"] == outputs["lake_key_arn"]
        assert outputs["lake_key_arn"] != outputs["templates_key_arn"]

    @pytest.mark.parametrize("bucket", ["lake_bucket", "client_bucket"])
    def test_public_access_is_fully_blocked(
        self, aws: boto3.Session, outputs: dict[str, Any], bucket: str
    ) -> None:
        block = aws.client("s3").get_public_access_block(Bucket=outputs[bucket])[
            "PublicAccessBlockConfiguration"
        ]
        assert all(block.values())


class TestLeastPrivilege:
    """IAM's policy simulator evaluates each role against the real table ARN and a key prefix."""

    @pytest.mark.parametrize(
        ("function", "action", "partition_key", "expected"),
        [
            ("scoring", "dynamodb:BatchGetItem", "USER#u1", "allowed"),
            ("scoring", "dynamodb:PutItem", "DEC#d1", "allowed"),
            # The scoring path must never be able to rewrite a behavioural profile.
            ("scoring", "dynamodb:PutItem", "USER#u1", "implicitDeny"),
            ("scoring", "dynamodb:DeleteItem", "USER#u1", "implicitDeny"),
            ("adaptation", "dynamodb:PutItem", "USER#u1", "allowed"),
            ("adaptation", "dynamodb:PutItem", "DEC#d1", "implicitDeny"),
            ("ledger", "dynamodb:GetItem", "USER#u1", "implicitDeny"),
            ("aggregator", "dynamodb:PutItem", "PAYEE#p1", "allowed"),
            ("aggregator", "dynamodb:PutItem", "AGG#u1", "allowed"),
            # Only adaptation writes profiles; the aggregator cannot reach the USER# partition.
            ("aggregator", "dynamodb:PutItem", "USER#u1", "implicitDeny"),
            ("scoring", "dynamodb:BatchGetItem", "AGG#u1", "allowed"),
            ("scoring", "dynamodb:PutItem", "AGG#u1", "implicitDeny"),
            ("scoring", "dynamodb:PutItem", "REPLAY#u1", "allowed"),
            ("aggregator", "dynamodb:PutItem", "REPLAY#u1", "implicitDeny"),
        ],
    )
    def test_each_role_reaches_only_its_own_prefixes(
        self,
        aws: boto3.Session,
        outputs: dict[str, Any],
        function: str,
        action: str,
        partition_key: str,
        expected: str,
    ) -> None:
        result = aws.client("iam").simulate_principal_policy(
            PolicySourceArn=outputs["function_role_arns"][function],
            ActionNames=[action],
            ResourceArns=[outputs["table_arn"]],
            ContextEntries=[
                {
                    "ContextKeyName": "dynamodb:LeadingKeys",
                    "ContextKeyValues": [partition_key],
                    "ContextKeyType": "stringList",
                }
            ],
        )
        assert result["EvaluationResults"][0]["EvalDecision"] == expected

    @pytest.mark.parametrize("function", ["scoring", "adaptation", "aggregator", "ledger"])
    @pytest.mark.parametrize(
        ("via_service", "expected"),
        [("dynamodb", "allowed"), ("s3", "implicitDeny")],
    )
    def test_table_roles_use_the_key_only_through_dynamodb(
        self,
        aws: boto3.Session,
        outputs: dict[str, Any],
        function: str,
        via_service: str,
        expected: str,
    ) -> None:
        # The table is under a customer-managed key, so a role without kms:Decrypt cannot read it
        # at all. The same permission must not let a role decrypt through any other service.
        result = aws.client("iam").simulate_principal_policy(
            PolicySourceArn=outputs["function_role_arns"][function],
            ActionNames=["kms:Decrypt"],
            ResourceArns=[outputs["templates_key_arn"]],
            ContextEntries=[
                {
                    "ContextKeyName": "kms:ViaService",
                    "ContextKeyValues": [f"{via_service}.{outputs['region']}.amazonaws.com"],
                    "ContextKeyType": "string",
                }
            ],
        )
        assert result["EvaluationResults"][0]["EvalDecision"] == expected
