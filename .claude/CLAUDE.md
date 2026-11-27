# AI ペルソナシステム

Amazon Bedrockを活用したAIペルソナ構築・議論シミュレーションシステム。

## コマンド

```bash
uv sync --extra dev              # 依存インストール
uv run python run_htmx.py        # ローカル起動
uv run pytest -q                 # 全テスト
uv run pytest -m unit -q         # 単体テスト
uv run pytest -m e2e             # 固定E2E回帰（起動中の:8000に実AWSで実行・約10分。既定の実行からは除外）
uv run ruff check --fix .        # リント自動修正
uv run mypy src/ web/            # 型チェック
./scripts/build-css.sh --minify  # CSSビルド
./deploy.sh                      # AWS へのデプロイ（CloudShell。閉域は --closed。cdk/README.md）
```

## テスト実行ルーティング

| 変更対象 | コマンド |
|---------|---------|
| src/managers/ | `uv run pytest tests/unit/ -q` |
| src/services/ | `uv run pytest tests/integration/ -q` |
| web/routers/ | `uv run pytest tests/api/ -q` |
| deploy.sh, scripts/*.sh, Dockerfile, run_htmx.py | `uv run pytest tests/infra/ -q` |
| web/templates/, 主要画面フロー | `uv run pytest -m e2e`（アプリ起動が前提。初回のみ `uv run playwright install chromium`） |
| cdk/ | `cd cdk && npx tsc --noEmit && npm test && npx cdk synth --no-staging` |

## 禁止事項

- `RemovalPolicy.DESTROY` の使用
- `.env` やAWS認証情報のコミット
- 本番リソースの削除（DynamoDB, S3, Cognito, CDKスタック）
- `git push --force` / `git reset --hard`
- `/pre-push-review` を実行せずに `git push` すること
- **main ブランチで直接作業すること。** 修正・追加を行う前に必ず新規ブランチを切る（例: `git checkout -b <type>/<topic>`）。YOU MUST NOT commit on main.

## 規約

- コミットメッセージ: `feat:`, `fix:`, `refactor:`, `doc:`, `test:` プレフィックス
- Python docstring: 英語
- コード内のコメント（`#`、`//`、`/* */`、`{# #}`、`<!-- -->`）: 英語。画面・プロンプト・エラー文などの文字列リテラルは対象外（日本語のまま）
- コメントに内部の仕様 ID（`U2`、`BR13.2`、`NFR1.33`、`Q3` など）を書かない。参照先が git に入っておらず、読み手が辿れないため。理由・制約をその場の英文で書く。変更履歴（「U2 で…に変えた」）も書かず、今の振る舞いと理由だけを書く。辿れるもの（ソース、テスト、環境変数名、`docs/` の文書）へのポインタは可
- 包括的言語を使用（master→main, whitelist→allowlist）
- デプロイ前: pytest + ruff + mypy 全パス必須。CDK変更時は `npx cdk diff` で確認

## 回答スタイル
- 挨拶、前置き・段落報告・絵文字禁止。結論ファースト
- 指摘すべきことは素直に指摘
