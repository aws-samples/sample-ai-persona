#!/usr/bin/env python3
"""
Script to create DynamoDB tables for AI Persona System.

This script creates the necessary DynamoDB tables with appropriate
Global Secondary Indexes (GSIs) for the AI Persona System.

Usage:
    python src/database/create_dynamodb_tables.py [--prefix PREFIX] [--region REGION]
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError


class DynamoDBTableCreator:
    """Creates DynamoDB tables for AI Persona System."""

    def __init__(
        self,
        table_prefix: str = "AIPersona",
        region: str = "us-east-1",
    ):
        """
        Initialize the table creator.

        Args:
            table_prefix: Prefix for table names
            region: AWS region
        """
        self.table_prefix = table_prefix
        self.region = region

        # Initialize DynamoDB client
        client_config = {"region_name": region}

        self.dynamodb = boto3.client("dynamodb", **client_config)

    def create_personas_table(self) -> bool:
        """
        Create the Personas table with GSIs.

        Returns:
            True if table was created, False if it already exists
        """
        table_name = f"{self.table_prefix}_Personas"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "type", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                    {"AttributeName": "name", "AttributeType": "S"},
                    {"AttributeName": "occupation", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "CreatedAtIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "NameIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "name", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "OccupationIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "occupation", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_discussions_table(self) -> bool:
        """
        Create the Discussions table with GSIs.

        Returns:
            True if table was created, False if it already exists
        """
        table_name = f"{self.table_prefix}_Discussions"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "type", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                    {"AttributeName": "topic", "AttributeType": "S"},
                    {"AttributeName": "mode", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "CreatedAtIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "TopicIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "topic", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "ModeIndex",
                        "KeySchema": [
                            {"AttributeName": "mode", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_uploaded_files_table(self) -> bool:
        """
        Create the UploadedFiles table with GSI.

        Returns:
            True if table was created, False if it already exists
        """
        table_name = f"{self.table_prefix}_UploadedFiles"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "type", "AttributeType": "S"},
                    {"AttributeName": "uploaded_at", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "UploadedAtIndex",
                        "KeySchema": [
                            {"AttributeName": "type", "KeyType": "HASH"},
                            {"AttributeName": "uploaded_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_datasets_table(self) -> bool:
        """Create the Datasets table."""
        table_name = f"{self.table_prefix}_Datasets"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_bindings_table(self) -> bool:
        """Create the PersonaDatasetBindings table with GSI."""
        table_name = f"{self.table_prefix}_PersonaDatasetBindings"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "persona_id", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "PersonaIdIndex",
                        "KeySchema": [
                            {"AttributeName": "persona_id", "KeyType": "HASH"}
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_knowledge_bases_table(self) -> bool:
        """Create the KnowledgeBases table."""
        table_name = f"{self.table_prefix}_KnowledgeBases"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_persona_kb_bindings_table(self) -> bool:
        """Create the PersonaKBBindings table with GSI."""
        table_name = f"{self.table_prefix}_PersonaKBBindings"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "persona_id", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "PersonaIdIndex",
                        "KeySchema": [
                            {"AttributeName": "persona_id", "KeyType": "HASH"}
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_survey_templates_table(self) -> bool:
        """Create the SurveyTemplates table."""
        table_name = f"{self.table_prefix}_SurveyTemplates"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "Feature", "Value": "MassSurvey"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def create_surveys_table(self) -> bool:
        """Create the Surveys table."""
        table_name = f"{self.table_prefix}_Surveys"

        try:
            self.dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
                SSESpecification={"Enabled": True, "SSEType": "KMS"},
                Tags=[
                    {"Key": "Application", "Value": "AIPersonaSystem"},
                    {"Key": "Feature", "Value": "MassSurvey"},
                    {"Key": "ManagedBy", "Value": "Python"},
                ],
            )
            print(f"✓ Created table: {table_name}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                print(f"⚠ Table already exists: {table_name}")
                return False
            else:
                print(f"✗ Error creating table {table_name}: {e}")
                raise

    def wait_for_table_active(self, table_name: str, max_wait: int = 300) -> bool:
        """
        Wait for a table to become active.

        Args:
            table_name: Name of the table
            max_wait: Maximum time to wait in seconds

        Returns:
            True if table is active, False if timeout
        """
        print(f"Waiting for table {table_name} to become active...")
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                response = self.dynamodb.describe_table(TableName=table_name)
                status = response["Table"]["TableStatus"]

                if status == "ACTIVE":
                    print(f"✓ Table {table_name} is now active")
                    return True

                time.sleep(5)

            except ClientError as e:
                print(f"✗ Error checking table status: {e}")
                return False

        print(f"✗ Timeout waiting for table {table_name} to become active")
        return False

    def create_all_tables(self, wait_for_active: bool = True) -> bool:
        """
        Create all tables for the AI Persona System.

        Args:
            wait_for_active: Whether to wait for tables to become active

        Returns:
            True if all tables were created successfully
        """
        print(f"\nCreating DynamoDB tables with prefix: {self.table_prefix}")
        print(f"Region: {self.region}")
        print()

        tables_created = []

        # Create Personas table
        if self.create_personas_table():
            tables_created.append(f"{self.table_prefix}_Personas")

        # Create Discussions table
        if self.create_discussions_table():
            tables_created.append(f"{self.table_prefix}_Discussions")

        # Create UploadedFiles table
        if self.create_uploaded_files_table():
            tables_created.append(f"{self.table_prefix}_UploadedFiles")

        # Create Datasets table
        if self.create_datasets_table():
            tables_created.append(f"{self.table_prefix}_Datasets")

        # Create PersonaDatasetBindings table
        if self.create_bindings_table():
            tables_created.append(f"{self.table_prefix}_PersonaDatasetBindings")

        # Create SurveyTemplates table
        if self.create_survey_templates_table():
            tables_created.append(f"{self.table_prefix}_SurveyTemplates")

        # Create Surveys table
        if self.create_surveys_table():
            tables_created.append(f"{self.table_prefix}_Surveys")

        # Create KnowledgeBases table
        if self.create_knowledge_bases_table():
            tables_created.append(f"{self.table_prefix}_KnowledgeBases")

        # Create PersonaKBBindings table
        if self.create_persona_kb_bindings_table():
            tables_created.append(f"{self.table_prefix}_PersonaKBBindings")

        # Wait for tables to become active
        if wait_for_active and tables_created:
            print()
            for table_name in tables_created:
                if not self.wait_for_table_active(table_name):
                    return False

        print("\n✓ All tables created successfully!")
        return True

    def list_tables(self) -> None:
        """List all tables with the configured prefix."""
        try:
            response = self.dynamodb.list_tables()
            tables = [
                t for t in response.get("Tables", []) if t.startswith(self.table_prefix)
            ]

            if tables:
                print(f"\nTables with prefix '{self.table_prefix}':")
                for table in tables:
                    print(f"  - {table}")
            else:
                print(f"\nNo tables found with prefix '{self.table_prefix}'")

        except ClientError as e:
            print(f"✗ Error listing tables: {e}")

    def delete_all_tables(self) -> bool:
        """
        Delete all tables with the configured prefix.

        WARNING: This will permanently delete all data!

        Returns:
            True if all tables were deleted successfully
        """
        print(
            f"\n⚠ WARNING: This will delete all tables with prefix '{self.table_prefix}'"
        )
        confirmation = input("Type 'DELETE' to confirm: ")

        if confirmation != "DELETE":
            print("Deletion cancelled")
            return False

        try:
            response = self.dynamodb.list_tables()
            tables = [
                t for t in response.get("Tables", []) if t.startswith(self.table_prefix)
            ]

            if not tables:
                print(f"No tables found with prefix '{self.table_prefix}'")
                return True

            for table_name in tables:
                print(f"Deleting table: {table_name}")
                self.dynamodb.delete_table(TableName=table_name)
                print(f"✓ Deleted table: {table_name}")

            print("\n✓ All tables deleted successfully!")
            return True

        except ClientError as e:
            print(f"✗ Error deleting tables: {e}")
            return False


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Create DynamoDB tables for AI Persona System"
    )
    parser.add_argument(
        "--prefix",
        default="AIPersona",
        help="Prefix for table names (default: AIPersona)",
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--list", action="store_true", help="List existing tables and exit"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete all tables with the specified prefix",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for tables to become active",
    )

    args = parser.parse_args()

    # Create table creator
    creator = DynamoDBTableCreator(
        table_prefix=args.prefix,
        region=args.region,
    )

    try:
        if args.list:
            creator.list_tables()
        elif args.delete:
            success = creator.delete_all_tables()
            sys.exit(0 if success else 1)
        else:
            success = creator.create_all_tables(wait_for_active=not args.no_wait)
            sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
