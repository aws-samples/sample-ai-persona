#!/usr/bin/env python3
"""
AIペルソナシステム - 起動スクリプト
"""

import uvicorn
import sys
from pathlib import Path
from dotenv import load_dotenv

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# .envファイルから環境変数を読み込み
load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "web.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["web", "src"],
    )
