"""Dataset analysis service — app-owned structured DuckDB analysis tool.

Provides the ``analyze_dataset`` tool (structured aggregation) that replaces the MotherDuck MCP.
The LLM is never given SQL, S3 paths, AWS credentials or local paths.
"""
