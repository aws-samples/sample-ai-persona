#!/usr/bin/env python3
"""
Bedrock Batch Inference用IAMロール作成スクリプト

BedrockがS3バケットにアクセスしてバッチ推論の入出力を行うためのIAMロールを作成する。
作成後、ロールARNを環境変数 BEDROCK_BATCH_ROLE_ARN に設定して使用する。

Usage:
    uv run python scripts/create_bedrock_batch_role.py
    uv run python scripts/create_bedrock_batch_role.py --bucket-name my-bucket
    uv run python scripts/create_bedrock_batch_role.py --role-name CustomRoleName
    uv run python scripts/create_bedrock_batch_role.py --delete
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_ROLE_NAME = "BedrockBatchInferenceRole"
DEFAULT_POLICY_NAME = "BedrockBatchInferenceAccess"


def get_account_id() -> str:
    """現在のAWSアカウントIDを取得する。"""
    sts = boto3.client("sts")
    return sts.get_caller_identity()["Account"]


def build_trust_policy() -> dict:
    """Bedrockサービスがこのロールを引き受けるための信頼ポリシーを構築する。"""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def build_policy(bucket_name: str) -> dict:
    """バッチ推論に必要なS3アクセス + Bedrockモデル呼び出しポリシーを構築する。"""
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3ReadInput",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                "Resource": [
                    bucket_arn,
                    f"{bucket_arn}/batch-inference/*",
                ],
            },
            {
                "Sid": "S3WriteOutput",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                ],
                "Resource": [
                    f"{bucket_arn}/batch-inference/output/*",
                ],
            },
            {
                "Sid": "BedrockInvokeModel",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                ],
                "Resource": [
                    "*",
                ],
            },
        ],
    }


def create_role(role_name: str, bucket_name: str) -> str:
    """IAMロールとポリシーを作成し、ロールARNを返す。"""
    iam = boto3.client("iam")

    # ロール作成
    try:
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(build_trust_policy()),
            Description="IAM role for Amazon Bedrock Batch Inference to access S3 input/output",
            Tags=[
                {"Key": "Project", "Value": "AIPersona"},
                {"Key": "Purpose", "Value": "BedrockBatchInference"},
            ],
        )
        role_arn = response["Role"]["Arn"]
        print(f"✓ ロール作成完了: {role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = f"arn:aws:iam::{get_account_id()}:role/{role_name}"
            print(f"ロールは既に存在します: {role_arn}")
        else:
            raise

    # インラインポリシーをアタッチ
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=DEFAULT_POLICY_NAME,
        PolicyDocument=json.dumps(build_policy(bucket_name)),
    )
    print(f"✓ ポリシーをアタッチ: {DEFAULT_POLICY_NAME}")
    print(f"  対象バケット: {bucket_name}")
    print("  権限: S3読み書き + Bedrock InvokeModel")

    return role_arn


def delete_role(role_name: str) -> None:
    """IAMロールとアタッチされたポリシーを削除する。"""
    iam = boto3.client("iam")

    try:
        # インラインポリシーを削除
        try:
            iam.delete_role_policy(
                RoleName=role_name,
                PolicyName=DEFAULT_POLICY_NAME,
            )
            print(f"✓ ポリシー削除: {DEFAULT_POLICY_NAME}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise

        # ロール削除
        iam.delete_role(RoleName=role_name)
        print(f"✓ ロール削除完了: {role_name}")

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            print(f"ロールが見つかりません: {role_name}")
        else:
            raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bedrock Batch Inference用IAMロールを作成する"
    )
    parser.add_argument(
        "--role-name",
        default=DEFAULT_ROLE_NAME,
        help=f"IAMロール名 (デフォルト: {DEFAULT_ROLE_NAME})",
    )
    parser.add_argument(
        "--bucket-name",
        default=None,
        help="S3バケット名 (デフォルト: src/config.pyのS3_BUCKET_NAME)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="ロールを削除する",
    )
    args = parser.parse_args()

    if args.delete:
        delete_role(args.role_name)
        return

    # バケット名の解決
    bucket_name = args.bucket_name
    if not bucket_name:
        bucket_name = __import__("os").getenv("S3_BUCKET_NAME")
    if not bucket_name:
        try:
            from src.config import Config

            bucket_name = Config().S3_BUCKET_NAME
        except Exception:
            pass
    if not bucket_name:
        print("エラー: S3バケット名を指定してください (--bucket-name)")
        sys.exit(1)

    role_arn = create_role(args.role_name, bucket_name)

    print()
    print("=" * 60)
    print("次のステップ:")
    print("  環境変数に以下を設定してください:")
    print(f"  export BEDROCK_BATCH_ROLE_ARN={role_arn}")
    print()
    print("  または .env ファイルに追加:")
    print(f"  BEDROCK_BATCH_ROLE_ARN={role_arn}")
    print("=" * 60)


if __name__ == "__main__":
    main()
