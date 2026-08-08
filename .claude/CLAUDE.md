# AI ペルソナシステム

Amazon Bedrockを活用したAIペルソナ構築・議論シミュレーションシステム。

## コマンド

```bash
uv sync --extra dev              # 依存インストール
uv run python run_htmx.py        # ローカル起動
uv run pytest -q                 # 全テスト
uv run pytest -m unit -q         # 単体テスト
uv run ruff check --fix .        # リント自動修正
uv run mypy src/ web/            # 型チェック
./scripts/build-css.sh --minify  # CSSビルド
cd cdk && npx cdk deploy --all   # CDKデプロイ
```

## テスト実行ルーティング

| 変更対象 | コマンド |
|---------|---------|
| src/managers/ | `uv run pytest tests/unit/ -q` |
| src/services/ | `uv run pytest tests/integration/ -q` |
| web/routers/ | `uv run pytest tests/api/ -q` |
| cdk/ | `cd cdk && npx tsc --noEmit && npx cdk synth --no-staging` |

## 禁止事項

- `RemovalPolicy.DESTROY` の使用
- `.env` やAWS認証情報のコミット
- 本番リソースの削除（DynamoDB, S3, Cognito, CDKスタック）
- `git push --force` / `git reset --hard`
- `/pre-push-review` を実行せずに `git push` すること

## 規約

- コミットメッセージ: `feat:`, `fix:`, `refactor:`, `doc:`, `test:` プレフィックス
- Python docstring: 英語
- 包括的言語を使用（master→main, whitelist→allowlist）
- デプロイ前: pytest + ruff + mypy 全パス必須。CDK変更時は `npx cdk diff` で確認

## 回答スタイル
- 挨拶、前置き・段落報告・絵文字禁止。結論ファースト
- 指摘すべきことは素直に指摘
