# AIペルソナシステム トラブルシューティングガイド

---

## ユーザー向け（アプリケーション操作に起因する問題）

### ファイルアップロードの問題

#### 対応ファイル形式一覧

| 用途 | 対応形式 | サイズ上限 |
|------|----------|-----------|
| ペルソナ生成 | `.txt`, `.md`, `.pdf`, `.docx`, `.doc`, `.csv` | 10MB |
| インタビュー（ファイル添付） | `.png`, `.jpg`, `.gif`, `.webp`, `.pdf`, `.txt`, `.csv`, `.html`, `.md` | 画像5MB / その他10MB |
| 議論ドキュメント | `.png`, `.jpg`, `.jpeg`, `.pdf` | 画像5MB / PDF 10MB / 合計32MB |
| ナレッジファイル | `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.txt`, `.md` | 10MB |
| マーケットレポート | `.pdf`, `.docx`, `.doc`, `.txt`, `.md`, `.csv` | 10MB |
| アンケート画像 | `.png`, `.jpg`, `.jpeg` | 5MB |

#### 問題: ペルソナ生成用ファイルのアップロードが失敗する

**対処法:**
1. ファイル形式の確認
   - 対応形式: `.txt`, `.md`, `.pdf`, `.docx`, `.doc`, `.csv`
   - サイズ上限: 10MB
   - テキストファイルのエンコーディング: UTF-8、Shift_JIS、EUC-JP
   - PDF/DOCXはmarkitdownで自動変換

#### 問題: ナレッジファイルのアップロードが失敗する

**対処法:**
1. ファイル形式の確認
   - 対応形式: `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.txt`, `.md`
   - サイズ上限: 10MB
2. ペルソナ詳細画面の「知識」タブからアップロードしているか確認

#### 問題: 議論用ドキュメントのアップロードが失敗する

**対処法:**
1. ファイル形式の確認
   - 対応形式: PNG, JPEG, PDF のみ
   - 個別ファイル: 画像5MB / PDF 10MB
   - 合計: 最大32MB（リクエストペイロード全体の上限）
2. ファイルの整合性確認
```bash
file --mime-type your_file.png
```

#### 問題: PDFドキュメントが正しく処理されない

**対処法:**
1. PDFが破損していないか確認
2. ページ数を確認（最大100ページ）
3. パスワード保護されていないか確認
4. テキストを含んでいるか確認（画像のみのPDFは非対応の場合あり）

---

### インタビューモードの問題

#### 問題: インタビューセッション作成エラー

**症状:**
- 「インタビューセッションの作成に失敗しました」エラー

**対処法:**
1. 最低1体のペルソナが選択されているか確認
2. ブラウザをリロードして再試行
3. 問題が続く場合は管理者に連絡（AWS認証情報の問題の可能性）

#### 問題: チャットの応答が返らない

**症状:**
- メッセージ送信後にローディングが終わらない
- 「ペルソナが回答を考えています...」が継続表示される

**対処法:**
1. ブラウザの開発者ツール（F12）でConsole/Networkタブを確認
2. ページをリロード（Cmd+Shift+R）
3. 問題が続く場合は新しいセッションを作成

#### 問題: セッション保存エラー

**対処法:**
1. セッション名が空でないか確認
2. メッセージが含まれているか確認（空のセッションは保存不可）

---

### マスアンケートの問題

#### 問題: アンケートが作成できない

**対処法:**
1. テンプレート名が空白のみになっていないか確認
2. 質問が1つ以上追加されているか確認
3. 選択式質問の場合、選択肢が2つ以上あるか確認
4. 画像は1枚まで添付可能

#### 問題: 対象ペルソナ数のエラー

**制限:**
- 最小: 100人以上
- 最大: 10,000人（画像付きの場合は1,000人まで）

画像付きアンケートでペルソナ数を増やしたい場合は、画像を削除してください。

#### 問題: アンケートが「実行中」のまま完了しない

**対処法:**
1. バッチ推論は通常10分〜数時間かかります。ペルソナ数が多いほど時間がかかります
2. 30分以上経っても完了しない場合は管理者に確認を依頼

#### 問題: CSVダウンロードが失敗する

**対処法:**
1. 署名付きURLの有効期限は5分です。期限切れの場合はページをリロード

#### 問題: カスタムCSVのアップロードが失敗する

**対処法:**
1. CSVファイル形式の確認
   - UTF-8エンコーディング
   - ヘッダー行が存在すること
   - カンマ区切り
2. ファイルサイズ: 最大100MB
3. 特殊文字やエスケープの確認
```bash
head -5 your_file.csv
wc -l your_file.csv
```

#### 問題: インサイトレポートの生成に失敗する

**対処法:**
1. しばらく時間をおいて再試行（API制限の可能性）
2. 問題が続く場合は管理者に連絡

---

### UI表示の問題

#### 問題: ページが更新されない

**症状:**
- ボタンをクリックしても反応がない
- フォーム送信後に画面が更新されない

**解決方法:**
1. ブラウザのキャッシュをクリア（Cmd+Shift+R）
2. ブラウザの開発者ツール（F12）でエラーを確認
3. htmxが読み込まれているか確認
```javascript
console.log(htmx.version);
```

#### 問題: リアルタイム表示（SSE）が動作しない

**症状:**
- 議論やインタビューのストリーミングが表示されない
- 接続エラーが表示される

**解決方法:**
1. ブラウザがSSEに対応しているか確認
```javascript
console.log('SSE supported:', typeof EventSource !== 'undefined');
```
2. プロキシやファイアウォールがSSE接続をブロックしていないか確認
3. ページをリロードして再試行

#### 問題: ストリーミング中に接続が切断される

**対処法:**
1. ネットワーク接続の安定性を確認
2. ページをリロードして再試行
3. プロキシ経由の場合は管理者にタイムアウト設定の確認を依頼

---

### 外部データセット連携の問題（実験的機能）

#### 問題: ペルソナがデータにアクセスできない

**対処法:**
1. ペルソナ詳細画面で紐付け設定を確認
2. 紐付けキー（user_id等）が正しいか確認
3. MCPサーバーが「起動中」か確認（設定ページ）
4. 不安定な場合はリトライ

---

### エラーメッセージ早見表（ユーザー向け）

#### ファイル関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| ファイル形式が正しくありません | 非対応形式 | [対応形式一覧](#対応ファイル形式一覧)を参照 |
| ファイルが大きすぎます | サイズ上限超過 | ファイルサイズを削減（上限は機能ごとに異なる） |
| 許可されていないファイル形式です | 議論ドキュメントで非対応形式 | .png, .jpg, .jpeg, .pdf を使用 |
| ファイルサイズが制限を超えています | 議論ドキュメント合計32MB超過 | ファイルサイズを削減、または添付数を減らす |

#### AI生成関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| ペルソナの生成に失敗しました | API接続エラー | 時間をおいて再試行。改善しない場合は管理者に連絡 |
| 議論の実行に失敗しました | レート制限/APIエラー | 時間をおいて再試行 |
| インサイトの生成に失敗しました | データ不足/APIエラー | 議論内容を確認し再試行 |

#### マスアンケート関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| テンプレート名は空白のみでは登録できません | テンプレート名が空白 | 有効な名前を入力 |
| 質問が1つも含まれていません | 質問未追加 | 質問を1つ以上追加 |
| 選択式質問には2つ以上の選択肢が必要です | 選択肢不足 | 選択肢を2つ以上追加 |
| 対象ペルソナ数は100以上で指定してください | ペルソナ数が100未満 | 100以上を指定 |
| 対象ペルソナ数は10000人までです | ペルソナ数が上限超過 | 10000以下を指定（画像付きの場合は1000まで） |
| 画像付きアンケートの場合、対象ペルソナ数は1000人までです | 画像付きで1000人超過 | 1000以下に減らすか画像を削除 |
| 画像は1枚まで添付できます | テンプレート画像数超過 | 画像を1枚に減らす |

---
---

## 管理者向け（AWS環境・インフラ設定に起因する問題）

### ローカル環境セットアップ

#### 問題: アプリケーションが起動しない

**症状:**
```
ModuleNotFoundError: No module named 'fastapi'
```

**解決方法:**
1. 依存関係を再インストール
```bash
uv sync
```

2. FastAPIが正しくインストールされているか確認
```bash
uv run python -c "import fastapi; print(fastapi.__version__)"
```

#### 問題: ポート8000が使用中

**症状:**
```
ERROR: [Errno 48] Address already in use
```

**解決方法:**
1. 別のポートで起動
```bash
uv run uvicorn web.main:app --reload --port 8001
```

2. 使用中のプロセスを確認して終了
```bash
lsof -i :8000
kill -9 <PID>
```

#### 問題: uvが見つからない

**解決方法:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

#### 問題: Python 3.13が見つからない

**解決方法:**
```bash
uv python install 3.13
uv python pin 3.13
```

#### 問題: 依存関係のインストールエラー

**解決方法:**
```bash
uv cache clean
uv sync --reinstall
```

#### 問題: Tailwind CSSのスタイルが適用されない

**解決方法:**
```bash
./scripts/build-css.sh --minify
ls -la web/static/css/
```

ブラウザのキャッシュもクリア（Cmd+Shift+R）してください。

---

### AWS認証・Bedrock接続

#### 問題: AWS認証エラー

**症状:**
- ペルソナ生成時に「認証エラー」が表示される
- 議論開始時にエラーが発生する

**解決方法:**
1. 認証情報の確認
```bash
aws sts get-caller-identity
```

2. 環境変数の確認
```bash
echo $AWS_REGION
echo $AWS_ACCESS_KEY_ID
```

3. IAM権限の確認
   - 必要な権限: `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`
   - バッチ推論使用時: `bedrock:CreateModelInvocationJob`, `bedrock:GetModelInvocationJob`

#### 問題: Bedrock APIレート制限

**症状:**
```
Rate limit exceeded
```

**解決方法:**
1. 少し時間をおいて再試行
2. 同時実行数を減らす
3. AWSサポートに制限緩和を依頼

---

### DynamoDB

#### 問題: DynamoDBテーブルが見つからない

**症状:**
```
DatabaseError: Required DynamoDB tables not found
```

**解決方法:**
1. CDKでバックエンドリソースがデプロイ済みか確認
```bash
cd cdk && npx cdk diff
```

2. AWS認証情報とリージョンを確認
```bash
aws sts get-caller-identity
echo $DYNAMODB_REGION
echo $DYNAMODB_TABLE_PREFIX
```

3. テーブルの存在確認
```bash
aws dynamodb list-tables --region $DYNAMODB_REGION | grep $DYNAMODB_TABLE_PREFIX
```

#### 問題: データベース接続エラー

**解決方法:**
1. 健全性チェックとDB情報の確認
```bash
uv run python -c "
from src.services.service_factory import service_factory
import json
db = service_factory.get_database_service()
print('健全性:', db.check_database_health())
print(json.dumps(db.get_database_info(), indent=2, ensure_ascii=False))
"
```

2. 環境変数の確認
```bash
cat .env | grep DYNAMODB
```

#### 問題: データベース検索が遅い

**解決方法:**
1. DynamoDBのキャパシティモードを確認
```bash
aws dynamodb describe-table --table-name ${DYNAMODB_TABLE_PREFIX}_Discussions --region $DYNAMODB_REGION
```

2. 必要に応じてオンデマンドモードへの切り替えを検討

---

### S3ストレージ

#### 問題: S3へのアップロードが失敗する

**対処法:**
1. AWS認証情報の確認
```bash
aws sts get-caller-identity
```

2. S3バケットの存在と権限確認
```bash
echo $S3_BUCKET_NAME
aws s3 ls s3://$S3_BUCKET_NAME/
```

3. 必要なIAM権限: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`

4. `AWS_REGION`がS3バケットのリージョンと一致しているか確認

#### 問題: アップロードディレクトリが存在しない（ローカル使用時）

**症状:**
```
FileNotFoundError: uploads directory not found
```

**解決方法:**
```bash
mkdir -p uploads
chmod 755 uploads
```

---

### マスアンケート（バッチ推論）

#### 問題: アンケート実行がエラーになる

**症状:**
- ステータスが「エラー」になる
- 「バッチ推論の実行に失敗しました」エラー

**対処法:**
1. IAMロールの確認
```bash
echo $BEDROCK_BATCH_ROLE_ARN
```
   - 未設定の場合はロールを作成:
   ```bash
   uv run python scripts/create_bedrock_batch_role.py --bucket-name $S3_BUCKET_NAME
   ```

2. S3バケットの確認
```bash
echo $S3_BUCKET_NAME
aws s3 ls s3://$S3_BUCKET_NAME/
```

3. ペルソナ数の確認
   - 最低100人以上が必要（Bedrock Batch Inferenceの制約）
   - 画像付きの場合は最大1000人、画像なしの場合は最大10000人

4. モデルアクセスの確認
   - バッチ推論用モデル（デフォルト: Claude Haiku）へのアクセス権限があるか確認
   - Bedrockコンソールの「モデルアクセス」で有効化されているか確認

#### 問題: アンケートが「実行中」のまま完了しない

**対処法:**
1. AWSコンソールでバッチ推論ジョブのステータスを確認
```bash
aws bedrock list-model-invocation-jobs --region $AWS_REGION --sort-by CreationTime --sort-order Descending --max-results 5
```

2. ジョブの詳細確認
```bash
aws bedrock get-model-invocation-job --job-identifier <job-arn> --region $AWS_REGION
```

3. ジョブが `Failed` の場合は出力先S3パスのエラーログを確認

#### 問題: ペルソナデータセットの読み込みが遅い

**対処法:**
1. 初回はHugging FaceからダウンロードしParquet形式でS3に配置するため30-120秒かかります
2. 2回目以降はS3上のParquetに直接クエリするため高速です
3. S3接続・AWS認証情報が有効か確認
4. DuckDB/Polarsがインストールされているか確認
```bash
uv run python -c "import duckdb; import polars; print('OK')"
```

---

### 長期記憶（AgentCore Memory）

#### 問題: 長期記憶が動作しない

**対処法:**
1. 環境変数の確認
```bash
echo $ENABLE_LONG_TERM_MEMORY    # true であること
echo $AGENTCORE_MEMORY_ID
echo $AGENTCORE_MEMORY_REGION
echo $SUMMARY_MEMORY_STRATEGY_ID
echo $SEMANTIC_MEMORY_STRATEGY_ID
```

2. AgentCore Memoryリソースが作成済みか確認
```bash
# CDK出力からMemory IDを取得
cd cdk && npx cdk outputs AgentcoreMemoryStack
```

3. IAMロールにAgentCore Memoryへのアクセス権限があるか確認
   - 必要な権限: `bedrock:InvokeAgent`（AgentCore Memory API）

---

### データ分析エージェント連携

#### 問題: データ分析エージェントの接続テストが失敗する

**対処法:**
1. 環境変数の確認
```bash
echo $DATA_AGENT_RUNTIME_ARN
echo $DATA_AGENT_ALIAS_ID
```

2. 連携先の [sample-text2sql-agent](https://github.com/aws-samples/sample-text2sql-agent) がデプロイ済みであること
3. IAMロールにBedrockエージェント呼び出し権限があるか確認
   - 必要な権限: `bedrock:InvokeAgent`
4. ARNのリージョンとアカウントIDが正しいか確認

詳細は [データ分析エージェント連携ガイド](data_agent_integration.md) を参照。

---

### 外部データセット連携（実験的機能）

#### 問題: MCPサーバーが起動しない

**対処法:**
1. uvxがインストールされているか確認
```bash
uvx --version
```

2. MotherDuck MCPサーバーの確認
```bash
uvx mcp-server-motherduck --help
```

3. 設定画面でデータセット連携が有効になっているか確認

#### 問題: データセットアップロードが失敗する

**対処法:**
1. CSVファイル形式: UTF-8、ヘッダー行あり、カンマ区切り
2. ファイルサイズ: 最大100MB
3. エンコーディングの変換
```bash
file -i your_file.csv
iconv -f SHIFT_JIS -t UTF-8 your_file.csv > your_file_utf8.csv
```

---

### SSE/プロキシ設定

#### 問題: ストリーミング中に接続が切断される（プロキシ環境）

**対処法:**
1. リバースプロキシのタイムアウト設定を調整
```nginx
# nginx設定例
proxy_read_timeout 300s;
proxy_buffering off;
```

2. ネットワークタブでSSE接続を確認
   - レスポンスタイプが `text/event-stream` であること

3. デバッグログで確認
```bash
uv run uvicorn web.main:app --reload --log-level debug
```

---

### エラーメッセージ早見表（管理者向け）

#### データベース関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| データの保存に失敗しました | DB接続エラー | DynamoDB接続確認 |
| データの取得に失敗しました | 権限不足 | IAM権限確認 |
| Required DynamoDB tables not found | テーブル未作成 | CDKデプロイを確認 |

#### バッチ推論関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| BEDROCK_BATCH_ROLE_ARN が設定されていません | IAMロール未設定 | 環境変数にロールARNを設定 |
| バッチ推論の実行に失敗しました | バッチ推論エラー | IAMロール・S3権限確認 |
| ペルソナデータセットの準備に失敗しました | S3/DuckDB接続エラー | S3権限・Parquetファイル確認 |

#### データセット連携関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| MCPサーバーの起動に失敗しました | uvx未インストール/権限不足 | uvxインストール確認 |
| CSVファイルの解析に失敗しました | 不正なCSV形式 | UTF-8エンコーディング確認 |
| データセットが見つかりません | 削除済み/ID不正 | データセット一覧を確認 |
| SQLクエリの実行に失敗しました | クエリエラー/認証問題 | リトライ、MCPサーバー再起動 |

---

### ログの確認方法

#### AWS環境（ECS Express Mode）

1. AWSコンソール → ECS → クラスター `ai-persona-cluster-<env>` を選択
2. サービス `ai-persona-<env>` → 「ログ」タブでコンテナログを確認
3. または、サービス詳細 → タスク → 個別タスクの「ログ」タブで特定タスクのログを確認

エラー発生時刻を絞り込んで検索するとトラブルシューティングが効率的です。

#### ローカル環境

```bash
# デバッグモードで起動
uv run uvicorn web.main:app --reload --log-level debug
```

---

### 緊急時の対処

#### システムが完全に動作しない場合

```bash
# 1. 仮想環境の削除と再構築
rm -rf .venv
uv cache clean
uv sync

# 2. CSSの再ビルド
./scripts/build-css.sh --minify

# 3. 環境変数の再設定
cp .env.example .env
# .env を編集してAWSリソース名等を設定

# 4. アプリケーション起動
uv run python run_htmx.py
```
