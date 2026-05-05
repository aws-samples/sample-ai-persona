#!/usr/bin/env python
"""
MCP Gateway 用 OpenAPI spec を生成する。

FastAPI の自動生成 spec から /api/mcp/ エンドポイントのみを抽出し、
AgentCore Gateway の OpenAPI Target に渡す JSON を出力する。

Usage:
    uv run python scripts/generate_mcp_openapi.py [--output path]
"""

import argparse
import copy
import json
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MCP_PATH_PREFIX = "/api/mcp/"


def generate_mcp_openapi(backend_url: str = "https://localhost") -> dict:
    from web.main import app

    full_spec = app.openapi()
    spec = copy.deepcopy(full_spec)

    # MCP エンドポイントのみ抽出
    spec["paths"] = {
        path: ops
        for path, ops in spec["paths"].items()
        if path.startswith(MCP_PATH_PREFIX)
    }

    # 使用されている $ref のスキーマだけ残す
    used_refs: set[str] = set()
    _collect_refs(spec["paths"], used_refs)
    if "components" in spec and "schemas" in spec["components"]:
        spec["components"]["schemas"] = {
            k: v
            for k, v in spec["components"]["schemas"].items()
            if k in used_refs
        }

    # メタ情報を調整
    spec["info"] = {
        "title": "AI Persona MCP API",
        "description": "AIペルソナシステムのMCPツール用REST API。AgentCore Gateway経由で外部AIエージェントから利用可能。",
        "version": full_spec["info"].get("version", "1.0.0"),
    }
    spec["servers"] = [{"url": backend_url}]

    return spec


def _collect_refs(obj: object, refs: set[str]) -> None:
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref_val = obj["$ref"]
            # "#/components/schemas/Foo" → "Foo"
            if ref_val.startswith("#/components/schemas/"):
                refs.add(ref_val.split("/")[-1])
        for v in obj.values():
            _collect_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_refs(item, refs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate MCP OpenAPI spec")
    parser.add_argument(
        "--output", "-o", default="openapi_mcp.json", help="Output file path"
    )
    parser.add_argument(
        "--backend-url",
        default="https://localhost",
        help="Backend URL for servers field",
    )
    args = parser.parse_args()

    spec = generate_mcp_openapi(backend_url=args.backend_url)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    print(f"Generated MCP OpenAPI spec: {output_path} ({len(spec['paths'])} endpoints)")


if __name__ == "__main__":
    main()
