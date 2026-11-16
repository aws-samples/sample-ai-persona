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
        # 手元のログイン（AUTH_MODE=local）は検証がないため、このマシンからだけ受ける
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=["web", "src"],
        # Dockerfile の CMD と同じ設定。uvicorn のアクセスログはクエリ（code・state）を
        # 出すため止め、アプリの http.access に置き換える
        access_log=False,
        timeout_keep_alive=310,
    )
